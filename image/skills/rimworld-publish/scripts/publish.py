"""On-demand Steam Workshop publish helper: validate a rimworld-release staging
folder and publish through the Steamworks SDK loaded from the local RimWorld
install. No Steam passwords, tokens, or accounts are ever handled - the desktop
client's logged-on session is reused directly."""

import argparse
import ctypes
import json
import os
import platform
import re
import struct
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

APPID = 294100
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
DEFAULT_RUN_DIR = Path.home() / ".rimworld-publish" / "steamworks"
DYLIB_ENV = "RIMWORLD_STEAM_DYLIB"
MAX_SCREENSHOTS = 20  # Workshop additional-preview limit
LANG_CODE_RE = re.compile(r"[a-z]{2,12}")

# 2026-11 实测（macOS arm64, RimWorld 自带 libsteam_api.dylib）：
# - SteamAPI_SteamUGC_v021 / SteamAPI_SteamUser_v023（无版本名符号不导出）
# - 回调结构体实际为 pack4：CreateItemResult_t(24B) 的 PublishedFileId 在偏移 4，
#   SubmitItemUpdateResult_t 三字段布局两种 pack 下相同（int@0, bool@4, uint64@8）
# - 回调 ID：SteamUGCQueryCompleted_t=3401, CreateItemResult_t=3403, SubmitItemUpdateResult_t=3404
# - SteamUGCDetails_t 的 rgchTitle 在偏移 24、rgchDescription 在偏移 154（pack4 无填充）
ERESULT = {
    1: "OK", 2: "Fail", 3: "NoConnection", 11: "AccountDisabled",
    15: "AccessDenied", 16: "Timeout", 21: "AccountNotFound", 25: "LimitExceeded",
    26: "AccountActivityLimit", 29: "InvalidParam", 32: "NotFound",
    42: "ServiceUnavailable", 84: "NotAuthorized", 86: "AccountNotFeatured",
}


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def find_dylib(override=None):
    """Locate RimWorld's bundled Steamworks library."""
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    env = os.environ.get(DYLIB_ENV)
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    common = Path.home() / "Library/Application Support/Steam/steamapps/common"
    system = platform.system()
    candidates = {
        "Darwin": [common / "RimWorld/RimWorld.app/Contents/MacOS/libsteam_api.dylib",
                   common / "RimWorld/RimWorldMac.app/Contents/PlugIns/steam_api.bundle/Contents/MacOS/libsteam_api.dylib"],
        "Windows": [common / "RimWorld/steam_api64.dll", common / "RimWorld/RimWorldWin64/steam_api64.dll"],
        "Linux": [common / "RimWorld/libsteam_api.so"],
    }.get(system, [])
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def image_size(path):
    """Return (width, height) for PNG/JPEG headers, else None; no imaging deps."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                i += 2
                continue
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return (width, height)
            i += 2 + seglen
    return None


class SteamworksError(RuntimeError):
    pass


class SteamworksUGC:
    """ctypes binding to RimWorld's libsteam_api, ManualDispatch (headless) mode.

    Context manager: writes steam_appid.txt into run_dir, chdir's there (paths
    passed to the setters must already be absolute), sets the SteamAppId env
    vars, then InitFlat's. SteamAPI_InitFlat is required - the no-arg
    SteamInternal_SteamAPI_Init returns false spuriously even when the client
    session is fine (2026-11 实测).
    """

    CB_QUERY_COMPLETED = 3401
    CB_CREATE_ITEM = 3403
    CB_SUBMIT_UPDATE = 3404

    def __init__(self, dylib, run_dir, timeout=600):
        self.dylib = Path(dylib)
        self.run_dir = Path(run_dir)
        self.timeout = timeout
        self._old_cwd = None
        self._lib = None
        self._pipe = None
        self._ugc = None

    def __enter__(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "steam_appid.txt").write_text(f"{APPID}\n", encoding="utf-8")
        self._old_cwd = os.getcwd()
        os.chdir(self.run_dir)
        for var in ("SteamAppId", "SteamGameId", "SteamOverlayGameId"):
            os.environ[var] = str(APPID)
        try:
            self._lib = ctypes.CDLL(str(self.dylib))
            self._bind()
            errbuf = ctypes.create_string_buffer(1024)
            if self._lib.SteamAPI_InitFlat(errbuf) != 0:
                raise SteamworksError(
                    f"SteamAPI_InitFlat 失败: {errbuf.value.decode(errors='replace')}；"
                    f"请确认桌面 Steam 客户端正在运行且已登录")
            self._lib.SteamAPI_ManualDispatch_Init()
            self._pipe = self._lib.SteamAPI_GetHSteamPipe()
            self._ugc = self._lib.SteamAPI_SteamUGC_v021()
            return self
        except Exception:
            os.chdir(self._old_cwd)
            raise

    def __exit__(self, *exc):
        try:
            if self._lib is not None:
                self._lib.SteamAPI_Shutdown()
        finally:
            os.chdir(self._old_cwd)
            self._old_cwd = None
        return False

    def _bind(self):
        lib = self._lib
        void_p = ctypes.c_void_p
        u64 = ctypes.c_uint64
        specs = {
            "SteamAPI_InitFlat": (ctypes.c_int, [ctypes.c_char_p]),
            "SteamAPI_ManualDispatch_Init": (None, []),
            "SteamAPI_ManualDispatch_RunFrame": (None, [ctypes.c_int]),
            "SteamAPI_GetHSteamPipe": (ctypes.c_int, []),
            "SteamAPI_Shutdown": (None, []),
            "SteamAPI_SteamUGC_v021": (void_p, []),
            "SteamAPI_SteamUser_v023": (void_p, []),
            "SteamAPI_ISteamUser_BLoggedOn": (ctypes.c_bool, [void_p]),
            "SteamAPI_ISteamUser_GetSteamID": (u64, [void_p]),
            "SteamAPI_ISteamUGC_CreateItem": (u64, [void_p, ctypes.c_uint32, ctypes.c_uint32]),
            "SteamAPI_ISteamUGC_StartItemUpdate": (u64, [void_p, ctypes.c_uint32, u64]),
            "SteamAPI_ISteamUGC_SetItemTitle": (ctypes.c_bool, [void_p, u64, ctypes.c_char_p]),
            "SteamAPI_ISteamUGC_SetItemDescription": (ctypes.c_bool, [void_p, u64, ctypes.c_char_p]),
            "SteamAPI_ISteamUGC_SetItemPreview": (ctypes.c_bool, [void_p, u64, ctypes.c_char_p]),
            "SteamAPI_ISteamUGC_SetItemContent": (ctypes.c_bool, [void_p, u64, ctypes.c_char_p]),
            "SteamAPI_ISteamUGC_SetItemVisibility": (ctypes.c_bool, [void_p, u64, ctypes.c_int]),
            "SteamAPI_ISteamUGC_SetItemUpdateLanguage": (ctypes.c_bool, [void_p, u64, ctypes.c_char_p]),
            "SteamAPI_ISteamUGC_AddItemPreviewFile": (ctypes.c_bool, [void_p, u64, ctypes.c_char_p, ctypes.c_int]),
            "SteamAPI_ISteamUGC_RemoveItemPreview": (ctypes.c_bool, [void_p, u64, ctypes.c_uint32]),
            "SteamAPI_ISteamUGC_SubmitItemUpdate": (u64, [void_p, u64, ctypes.c_char_p]),
            "SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest": (u64, [void_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint32]),
            "SteamAPI_ISteamUGC_SetLanguage": (ctypes.c_bool, [void_p, u64, ctypes.c_char_p]),
            "SteamAPI_ISteamUGC_SetReturnAdditionalPreviews": (ctypes.c_bool, [void_p, u64, ctypes.c_bool]),
            "SteamAPI_ISteamUGC_SendQueryUGCRequest": (u64, [void_p, u64]),
            "SteamAPI_ISteamUGC_GetQueryUGCResult": (ctypes.c_bool, [void_p, u64, ctypes.c_uint32, void_p]),
            "SteamAPI_ISteamUGC_GetQueryUGCPreviewURL": (ctypes.c_bool, [void_p, u64, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]),
            "SteamAPI_ISteamUGC_GetQueryUGCNumAdditionalPreviews": (ctypes.c_uint32, [void_p, u64, ctypes.c_uint32]),
            "SteamAPI_ISteamUGC_GetQueryUGCAdditionalPreview": (ctypes.c_bool, [void_p, u64, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_int)]),
            "SteamAPI_ISteamUGC_ReleaseQueryUGCRequest": (ctypes.c_bool, [void_p, u64]),
            "SteamAPI_ManualDispatch_GetAPICallResult": (ctypes.c_bool, [ctypes.c_int, u64, void_p, ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_bool)]),
        }
        for name, (restype, argtypes) in specs.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, argtypes

    # -- session --

    def logged_on(self):
        return bool(self._lib.SteamAPI_ISteamUser_BLoggedOn(self._lib.SteamAPI_SteamUser_v023()))

    def steam_id(self):
        return self._lib.SteamAPI_ISteamUser_GetSteamID(self._lib.SteamAPI_SteamUser_v023())

    # -- pump / callbacks --

    def _wait_apicall(self, apicall, cb_id, buf_size, label, timeout=None):
        buf = ctypes.create_string_buffer(buf_size)
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            self._lib.SteamAPI_ManualDispatch_RunFrame(self._pipe)
            iofail = ctypes.c_bool(False)
            if self._lib.SteamAPI_ManualDispatch_GetAPICallResult(
                    self._pipe, ctypes.c_uint64(apicall), buf, buf_size, cb_id, ctypes.byref(iofail)):
                return buf.raw
            if iofail.value:
                raise SteamworksError(f"{label}: 调用发生 IO 失败")
            time.sleep(0.2)
        raise SteamworksError(f"{label}: 等待结果超时（{int(timeout or self.timeout)}s）")

    @staticmethod
    def _eresult(code):
        return f"{code}({ERESULT.get(code, '?')})"

    # -- item lifecycle --

    def create_item(self):
        apicall = self._lib.SteamAPI_ISteamUGC_CreateItem(self._ugc, APPID, 0)
        raw = self._wait_apicall(apicall, self.CB_CREATE_ITEM, 24, "CreateItem", 120)
        result = struct.unpack_from("<i", raw, 0)[0]
        if result != 1:
            raise SteamworksError(f"CreateItem 失败: {self._eresult(result)}")
        # pack4 实测：ID 在偏移 4；若量级不合理再尝试 pack8 偏移 8
        file_id = struct.unpack_from("<Q", raw, 4)[0]
        if not 10**8 < file_id < 10**13:
            file_id = struct.unpack_from("<Q", raw, 8)[0]
        needs_accept = raw[12] != 0
        return file_id, needs_accept

    def start_update(self, file_id):
        handle = self._lib.SteamAPI_ISteamUGC_StartItemUpdate(self._ugc, APPID, file_id)
        if not handle:
            raise SteamworksError("StartItemUpdate 返回空句柄")
        return handle

    def _setter(self, name, handle, value):
        fn = getattr(self._lib, f"SteamAPI_ISteamUGC_{name}")
        if not fn(self._ugc, handle, value):
            raise SteamworksError(f"{name} 被拒绝（值过长或无效?）")

    def set_title(self, handle, text):
        self._setter("SetItemTitle", handle, text.encode("utf-8"))

    def set_description(self, handle, text):
        self._setter("SetItemDescription", handle, text.encode("utf-8"))

    def set_preview(self, handle, path):
        self._setter("SetItemPreview", handle, str(Path(path).resolve()).encode())

    def set_content(self, handle, path):
        # 必须指向 mod 文件夹本身：物品根 = 该目录内容（About/ 须在物品根，2026-11 实测）
        self._setter("SetItemContent", handle, str(Path(path).resolve()).encode())

    def set_visibility(self, handle, value):
        if not self._lib.SteamAPI_ISteamUGC_SetItemVisibility(self._ugc, handle, value):
            raise SteamworksError(f"SetItemVisibility({value}) 被拒绝")

    def set_update_language(self, handle, language):
        if not self._lib.SteamAPI_ISteamUGC_SetItemUpdateLanguage(self._ugc, handle, language.encode()):
            raise SteamworksError(f"SetItemUpdateLanguage({language}) 被拒绝（语言码无效?）")

    def add_screenshot(self, handle, path):
        if not self._lib.SteamAPI_ISteamUGC_AddItemPreviewFile(
                self._ugc, handle, str(Path(path).resolve()).encode(), 0):
            raise SteamworksError(f"AddItemPreviewFile 失败: {path}")

    def remove_screenshot(self, handle, index):
        # 从高索引往低删，避免移位
        self._lib.SteamAPI_ISteamUGC_RemoveItemPreview(self._ugc, handle, index)

    def submit(self, handle, changenote=""):
        apicall = self._lib.SteamAPI_ISteamUGC_SubmitItemUpdate(
            self._ugc, handle, (changenote or "").encode("utf-8"))
        raw = self._wait_apicall(apicall, self.CB_SUBMIT_UPDATE, 16, "SubmitItemUpdate")
        result = struct.unpack_from("<i", raw, 0)[0]
        needs_accept = raw[4] != 0
        file_id = struct.unpack_from("<Q", raw, 8)[0]
        if result != 1:
            raise SteamworksError(f"SubmitItemUpdate 失败: {self._eresult(result)}"
                                  + ("；预览图超 1MB 硬上限" if result == 25 else ""))
        return {"result": result, "needsAccept": needs_accept, "fileId": file_id}

    # -- query (readback) --

    def query_details(self, file_id, language="english"):
        ids = (ctypes.c_uint64 * 1)(file_id)
        q = self._lib.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest(self._ugc, ids, 1)
        try:
            self._lib.SteamAPI_ISteamUGC_SetLanguage(self._ugc, q, language.encode())
            self._lib.SteamAPI_ISteamUGC_SetReturnAdditionalPreviews(self._ugc, q, True)
            apicall = self._lib.SteamAPI_ISteamUGC_SendQueryUGCRequest(self._ugc, q)
            self._wait_apicall(apicall, self.CB_QUERY_COMPLETED, 64, "SendQueryUGCRequest", 120)
            # 布局无关解析：缓冲远大于 sizeof(SteamUGCDetails_t)
            details = ctypes.create_string_buffer(65536)
            if not self._lib.SteamAPI_ISteamUGC_GetQueryUGCResult(self._ugc, q, 0, details):
                raise SteamworksError("GetQueryUGCResult 失败（物品不存在?）")
            raw = details.raw

            def cstr(off, cap):
                chunk = raw[off:off + cap]
                return chunk.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

            # pack4 实测偏移：rgchTitle@24(129B = k_cchPublishedDocumentTitleMax)，
            # rgchDescription@153(8000B)，eVisibility@8176（2026-11 读回校准）
            visibility = struct.unpack_from("<i", raw, 8176)[0]
            if visibility not in (0, 1, 2):
                visibility = None
            url = ctypes.create_string_buffer(512)
            self._lib.SteamAPI_ISteamUGC_GetQueryUGCPreviewURL(self._ugc, q, 0, url, 512)
            screenshots = []
            n = self._lib.SteamAPI_ISteamUGC_GetQueryUGCNumAdditionalPreviews(self._ugc, q, 0)
            for i in range(n):
                u2 = ctypes.create_string_buffer(512)
                orig = ctypes.create_string_buffer(256)
                ptype = ctypes.c_int(0)
                if self._lib.SteamAPI_ISteamUGC_GetQueryUGCAdditionalPreview(
                        self._ugc, q, 0, i, u2, 512, orig, 256, ctypes.byref(ptype)):
                    screenshots.append({"url": u2.value.decode(errors="replace"),
                                        "originalFileName": orig.value.decode(errors="replace"),
                                        "type": ptype.value})
            return {"publishedFileId": file_id, "language": language,
                    "title": cstr(24, 129), "description": cstr(153, 8000),
                    "visibility": visibility, "previewUrl": url.value.decode(errors="replace"),
                    "screenshotCount": n, "screenshots": screenshots}
        finally:
            self._lib.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest(self._ugc, q)


def preflight(staging, project=None, preview=None):
    issues = []
    staging = Path(staging)
    if not staging.is_dir():
        issues.append(f"staging 目录不存在: {staging}")
    project = Path(project) if project else None
    if project and not project.is_dir():
        issues.append(f"project 目录不存在: {project}")

    mod = name = None
    version = None
    supported = []
    if staging.is_dir():
        mods = [p for p in sorted(staging.iterdir())
                if p.is_dir() and (p / "About" / "About.xml").is_file()]
        if not mods and (staging / "About" / "About.xml").is_file():
            # staging 直接就是 mod 文件夹（About/About.xml 在其下）同样合法
            mods = [staging]
        if len(mods) != 1:
            issues.append(f"staging 应恰含一个带 About/About.xml 的 mod 文件夹，找到 {len(mods)} 个: "
                          + str([p.name for p in mods]))
        else:
            mod = mods[0]
            root = ET.parse(mod / "About" / "About.xml").getroot()
            name = (root.findtext("name") or "").strip() or None
            version = (root.findtext("modVersion") or "").strip() or None
            supported = [(li.text or "").strip() for li in root.findall("supportedVersions/li")]
            if not name:
                issues.append("About.xml 缺少 <name>，无法生成工坊标题")

    preview_path = preview
    if mod and preview_path is None:
        for cand in (mod / "About" / "Preview.png", mod / "About" / "Preview.jpg"):
            if cand.is_file():
                preview_path = cand
                break
    preview_info = {"path": str(preview_path) if preview_path else None, "ok": False,
                    "width": None, "height": None}
    if preview_path is None:
        issues.append("未找到预览图（About/Preview.png 或 .jpg）")
    else:
        size = image_size(Path(preview_path))
        if size is None:
            issues.append(f"预览图无法解析尺寸（需 PNG/JPG）: {preview_path}")
        else:
            preview_info.update(width=size[0], height=size[1])
            if size[0] < 512 or size[1] < 512:
                issues.append(f"预览图 {size[0]}x{size[1]} 低于最低 512x512")
            else:
                preview_info["ok"] = True
            # Steam 工坊 preview 硬上限 1MB，超限时 SubmitItemUpdate 报 LimitExceeded；
            # JPEG 内容配 .png 文件名可用（工坊按内容识别）。
            size_bytes = Path(preview_path).stat().st_size
            preview_info["sizeBytes"] = size_bytes
            if size_bytes > 1_000_000:
                issues.append(f"预览图 {size_bytes} 字节超过工坊 1MB 上限；可转 JPEG 压缩，扩展名无需更改")

    published_id = None
    for base in ([mod / "About"] if mod else []) + ([project / "About"] if project else []):
        marker = base / "PublishedFileId.txt"
        if marker.is_file():
            value = marker.read_text(encoding="utf-8").strip()
            if value.isdigit():
                published_id = value
                break
            issues.append(f"PublishedFileId.txt 内容不是数字: {value!r}")

    dylib = find_dylib()
    if dylib is None:
        issues.append("未找到 RimWorld 自带的 Steamworks 库（libsteam_api.dylib / steam_api64.dll / "
                      f"libsteam_api.so）；请确认通过 Steam 安装了 RimWorld，或设置环境变量 {DYLIB_ENV}")

    public_page = read_public_sources(project) if project else None

    return {
        "passed": not issues,
        "staging": str(staging),
        "modFolder": mod.name if mod else None,
        "modPath": str(mod) if mod else None,
        "modName": name,
        "modVersion": version,
        "supportedVersions": supported,
        "preview": preview_info,
        "publishedFileId": published_id,
        "mode": "update" if published_id else "create",
        "steamLibrary": str(dylib) if dylib else None,
        "publicPage": ({"dir": str(public_page["dir"]),
                        "languages": sorted(public_page["languages"]),
                        "screenshots": len(public_page["screenshots"])}
                       if public_page else None),
        "issues": issues,
    }


def read_public_sources(project):
    """Parse <project>/public/ workshop-page assets.

    public/<语言码>.txt  - 首行标题，空行后为 BBCode 描述
    public/Screenshots/  - png/jpg 截图，按文件名排序
    public/ 不进发行包（rimworld-release 排除），仅作为工坊页面素材。
    """
    project = Path(project)
    public = project / "public"
    if not public.is_dir():
        return None
    languages = {}
    for txt in sorted(public.glob("*.txt")):
        code = txt.stem.lower()
        if not LANG_CODE_RE.fullmatch(code):
            raise ValueError(f"public/ 语言文件名须为 Steam 语言码（如 schinese.txt）: {txt.name}")
        text = txt.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"public/{txt.name} 为空")
        lines = text.splitlines()
        title = lines[0].strip()
        description = "\n".join(lines[1:]).lstrip("\n")
        if not title:
            raise ValueError(f"public/{txt.name} 首行标题为空")
        languages[code] = {"title": title, "description": description}
    shots_dir = public / "Screenshots"
    screenshots = sorted([p for p in shots_dir.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
                         ) if shots_dir.is_dir() else []
    if len(screenshots) > MAX_SCREENSHOTS:
        raise ValueError(f"public/Screenshots 截图 {len(screenshots)} 张超过工坊上限 {MAX_SCREENSHOTS}")
    return {"dir": public, "languages": languages, "screenshots": screenshots}


def _run_submission(sdk, file_id, setup, changenote="", label="submit"):
    """One independent update+submit; retry once on generic Fail.

    每笔提交独立生效，失败重做整笔安全；实测偶发 EResult 2(Fail)（Steam 端瞬时
    拒绝，紧随其后的重试即成功），故对通用失败自动重试一次，其余错误照常抛出。
    """
    last = None
    for attempt in (1, 2):
        try:
            handle = sdk.start_update(file_id)
            setup(handle)
            return sdk.submit(handle, changenote)
        except SteamworksError as ex:
            last = ex
            if attempt == 1 and "2(Fail)" in str(ex):
                time.sleep(2)
                continue
            raise
    raise last


def _apply_language_slots(sdk, file_id, languages, changenote=""):
    """One update+submit per language slot (实测路径：每个语言一次独立提交)。"""
    steps = []
    for code in sorted(languages):
        data = languages[code]

        def setup(handle, data=data, code=code):
            sdk.set_update_language(handle, code)
            sdk.set_title(handle, data["title"])
            sdk.set_description(handle, data["description"])

        _run_submission(sdk, file_id, setup, label=f"slot {code}")
        steps.append(f"slot {code}: OK")
    return steps


def _replace_screenshots(sdk, file_id, screenshots):
    """Snapshot semantics: the local list is the source of truth.

    空列表且线上已有截图时跳过（防误清空）；非空时先从高索引删除线上全部再按序追加。
    """
    online = sdk.query_details(file_id)["screenshotCount"]
    if not screenshots:
        return [f"screenshots: skipped (本地为空, 线上 {online} 张)"] if online else []

    def setup(handle):
        for index in range(online - 1, -1, -1):
            sdk.remove_screenshot(handle, index)
        for shot in screenshots:
            sdk.add_screenshot(handle, shot)

    _run_submission(sdk, file_id, setup, label="screenshots")
    return [f"screenshots: replaced {online} -> {len(screenshots)}"]


def _open_sdk(args):
    dylib = find_dylib(getattr(args, "dylib", None))
    if dylib is None:
        raise ValueError("未找到 RimWorld 的 Steamworks 库；请通过 Steam 安装 RimWorld 或设置 "
                         f"{DYLIB_ENV}（详见 sdk-env.md）")
    return SteamworksUGC(dylib, args.run_dir, timeout=getattr(args, "timeout", 600) or 600)


def _require_logged_on(sdk):
    if not sdk.logged_on():
        return {"ok": False, "reason": "not-logged-on",
                "detail": "桌面 Steam 客户端未登录；请打开客户端登录后重试（无需输入密码给本工具）"}
    return None


def cmd_preflight(args):
    return preflight(args.staging, args.project, args.preview)


def cmd_session(args):
    with _open_sdk(args) as sdk:
        logged = sdk.logged_on()
        return {"ok": logged, "dylib": str(sdk.dylib), "appId": APPID,
                "steamId": str(sdk.steam_id()) if logged else None,
                "detail": None if logged else "桌面 Steam 客户端未登录"}


def cmd_upload(args):
    check = preflight(args.staging, args.project, args.preview)
    if not check["passed"]:
        return {"ok": False, "reason": "preflight-failed", "issues": check["issues"]}

    public = read_public_sources(args.project) if args.project else None
    english = (public or {}).get("languages", {}).get("english")
    title = args.title or check["modName"]
    description = args.description if args.description is not None else (english or {}).get("description", "")

    steps = []
    needs_accept = False
    with _open_sdk(args) as sdk:
        blocked = _require_logged_on(sdk)
        if blocked:
            return blocked
        if check["publishedFileId"]:
            file_id = int(check["publishedFileId"])
            mode = "update"
        else:
            file_id, needs_accept = sdk.create_item()
            mode = "create"
        # 主提交：内容 + 主图 + 英文槽 + 可见性 + 变更说明
        def main_setup(handle):
            sdk.set_title(handle, title)
            sdk.set_description(handle, description)
            sdk.set_preview(handle, check["preview"]["path"])
            sdk.set_content(handle, check["modPath"])
            sdk.set_visibility(handle, args.visibility)

        submit = _run_submission(sdk, file_id, main_setup, args.changenote or "", "main")
        needs_accept = needs_accept or submit["needsAccept"]
        steps.append("content+preview+english: OK")

        if public:
            remaining = {code: data for code, data in public["languages"].items() if code != "english"}
            steps += _apply_language_slots(sdk, file_id, remaining)
            steps += _replace_screenshots(sdk, file_id, public["screenshots"])

    result = {"ok": True, "mode": mode, "publishedFileId": str(file_id),
              "workshopUrl": WORKSHOP_URL.format(file_id), "steps": steps,
              "visibility": args.visibility, "needsAccept": needs_accept}
    if mode == "create":
        result["nextStep"] = (f"运行 adopt --project <工程根> --id {file_id} 回写 About/PublishedFileId.txt；"
                              "首次发布建议保持私密，网页确认并接受工坊协议后再公开")
    if needs_accept:
        result["legalAgreement"] = "需要用户在工坊网页接受 Steam Workshop 法律协议，物品在此之前保持隐藏"
    return result


def cmd_page(args):
    public = read_public_sources(args.project)
    if not public or (not public["languages"] and not public["screenshots"]):
        return {"ok": False, "reason": "no-assets",
                "detail": f"{public['dir'] if public else Path(args.project) / 'public'} 下无语言文件或截图"}
    file_id = int(args.id) if args.id else None
    if file_id is None:
        marker = Path(args.project) / "About" / "PublishedFileId.txt"
        if not marker.is_file():
            return {"ok": False, "reason": "no-id",
                    "detail": f"未提供 --id 且 {marker} 不存在"}
        file_id = int(marker.read_text(encoding="utf-8").strip())

    steps = []
    with _open_sdk(args) as sdk:
        blocked = _require_logged_on(sdk)
        if blocked:
            return blocked
        if public["languages"]:
            steps += _apply_language_slots(sdk, file_id, public["languages"])
        if public["screenshots"]:
            steps += _replace_screenshots(sdk, file_id, public["screenshots"])
    return {"ok": True, "publishedFileId": str(file_id),
            "workshopUrl": WORKSHOP_URL.format(file_id), "steps": steps}


def cmd_inspect(args):
    with _open_sdk(args) as sdk:
        blocked = _require_logged_on(sdk)
        if blocked:
            return blocked
        info = sdk.query_details(int(args.id), args.language)
    info["visibilityMeaning"] = {0: "public", 1: "friends-only", 2: "private"}.get(info["visibility"])
    info["ok"] = bool(info["title"])
    return info


def cmd_adopt(args):
    if args.file is None and args.project is None:
        raise ValueError("需要 --project 或 --file 之一")
    item_id = args.id.strip()
    if not item_id.isdigit():
        raise ValueError(f"物品 ID 必须为数字: {args.id}")
    target = args.file if args.file is not None else args.project / "About" / "PublishedFileId.txt"
    if not target.parent.exists():
        raise ValueError(f"About 目录不存在: {target.parent}；用 --file 指定实际位置")
    existing = target.read_text(encoding="utf-8").strip() if target.exists() else None
    if existing == item_id:
        return {"adopted": True, "already": True, "path": str(target), "id": item_id}
    if existing is not None and not args.force:
        return {"adopted": False, "reason": "id-mismatch", "existing": existing,
                "path": str(target), "id": item_id}
    target.write_text(item_id + "\n", encoding="utf-8")
    return {"adopted": True, "already": False, "path": str(target), "id": item_id}


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_steam_options(p):
        p.add_argument("--dylib", type=Path, help="Steamworks 库路径（默认自动探测 RimWorld 安装）")
        p.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR,
                       help=f"steam_appid.txt 运行目录（默认 {DEFAULT_RUN_DIR}）")
        p.add_argument("--timeout", type=int, default=600)

    pre = sub.add_parser("preflight", help="核对发行产物、Steamworks 库与 public/ 页面素材，无需客户端")
    pre.add_argument("--staging", required=True, type=Path)
    pre.add_argument("--project", type=Path)
    pre.add_argument("--preview", type=Path)

    ses = sub.add_parser("session", help="检查 Steamworks 会话（桌面客户端登录状态），无需配置")
    add_steam_options(ses)

    up = sub.add_parser("upload", help="SDK 上传内容+页面素材，需用户上传授权")
    up.add_argument("--staging", required=True, type=Path)
    up.add_argument("--project", type=Path)
    up.add_argument("--preview", type=Path)
    up.add_argument("--title")
    up.add_argument("--description")
    up.add_argument("--changenote")
    up.add_argument("--visibility", type=int, choices=(0, 1, 2), default=2,
                    help="0 公开 / 1 好友 / 2 私密（默认私密，首次发布两段式）")
    add_steam_options(up)

    pg = sub.add_parser("page", help="仅更新工坊页面（多语言槽+截图），不动 mod 内容")
    pg.add_argument("--project", required=True, type=Path)
    pg.add_argument("--id", help="物品 ID（默认读工程 About/PublishedFileId.txt）")
    add_steam_options(pg)

    ins = sub.add_parser("inspect", help="只读查询物品页面（按语言），作为发布后自验证")
    ins.add_argument("--id", required=True)
    ins.add_argument("--language", default="english")
    add_steam_options(ins)

    ad = sub.add_parser("adopt", help="把物品 ID 回写工程 About/PublishedFileId.txt")
    ad.add_argument("--project", type=Path)
    ad.add_argument("--file", type=Path)
    ad.add_argument("--id", required=True)
    ad.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    handlers = {
        "preflight": (cmd_preflight, "passed"),
        "session": (cmd_session, "ok"),
        "upload": (cmd_upload, "ok"),
        "page": (cmd_page, "ok"),
        "inspect": (cmd_inspect, "ok"),
        "adopt": (cmd_adopt, "adopted"),
    }
    try:
        func, ok_key = handlers[args.command]
        result = func(args)
        emit(result)
        return 0 if result.get(ok_key) else 1
    except (OSError, ValueError, KeyError, ET.ParseError, SteamworksError) as ex:
        emit({"error": str(ex)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

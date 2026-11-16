"""On-demand Steam Workshop publish helper: validate a rimworld-release staging
folder, build the Workshop VDF, and drive an isolated steamcmd; Steam passwords
are never read or stored, and the login token stays in steamcmd's own config."""

import argparse
import json
import os
import platform
import re
import struct
import subprocess
import sys
import tarfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

APPID = "294100"
DEFAULT_ROOT = Path.home() / ".rimworld-publish"
DOWNLOADS = {
    "Darwin": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_osx.tar.gz",
    "Linux": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz",
    "Windows": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip",
}
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def tail(text, size=800):
    return text[-size:] if text else ""


def guard_steamhome(steamhome):
    """Refuse any steamhome that overlaps the real user Steam installation."""
    target = steamhome.resolve()
    home = Path.home().resolve()
    real_dirs = [
        home,
        home / "Library/Application Support/Steam",
        home / ".steam",
        home / ".local/share/Steam",
    ]
    for real in real_dirs:
        if target == real or real.is_relative_to(target):
            raise ValueError(f"steamhome 不能覆盖真实 Steam 目录: {steamhome}")


def load_config(path):
    cfg = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    for key in ("steamcmd", "steamhome", "account"):
        if not cfg.get(key):
            raise ValueError(f"配置缺少 {key}: {path}")
    steamhome = Path(cfg["steamhome"]).expanduser()
    guard_steamhome(steamhome)
    return {"steamcmd": Path(cfg["steamcmd"]).expanduser(), "steamhome": steamhome, "account": cfg["account"]}


def steamcmd_argv(steamcmd):
    argv = [str(steamcmd)]
    if sys.platform == "darwin" and platform.machine() == "arm64":
        argv = ["arch", "-x86_64"] + argv
    return argv


def run_steamcmd(config, commands, timeout):
    env = dict(os.environ)
    env["HOME"] = str(config["steamhome"])
    proc = subprocess.run(
        steamcmd_argv(config["steamcmd"]) + commands,
        env=env, capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    return proc.stdout + proc.stderr


def classify_login(output):
    lower = output.lower()
    if "logged in ok" in lower:
        return "ok"
    if "cached credentials not found" in lower or "password" in lower:
        return "relogin"
    return "unknown"


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

    published_id = None
    for base in ([mod / "About"] if mod else []) + ([project / "About"] if project else []):
        marker = base / "PublishedFileId.txt"
        if marker.is_file():
            value = marker.read_text(encoding="utf-8").strip()
            if value.isdigit():
                published_id = value
                break
            issues.append(f"PublishedFileId.txt 内容不是数字: {value!r}")

    return {
        "passed": not issues,
        "staging": str(staging),
        "modFolder": mod.name if mod else None,
        "modName": name,
        "modVersion": version,
        "supportedVersions": supported,
        "preview": preview_info,
        "publishedFileId": published_id,
        "mode": "update" if published_id else "create",
        "issues": issues,
    }


def escape_vdf(value):
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "").replace("\t", "\\t"))


def vdf_text(value):
    """Escape text for VDF; accept both real newlines and literal \\n."""
    return escape_vdf(value.replace("\\n", "\n"))


def read_vdf_id(vdf_path):
    match = re.search(r'"publishedfileid"\s*"(\d+)"', vdf_path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def cmd_preflight(args):
    return preflight(args.staging, args.project, args.preview)


def cmd_install(args):
    system = platform.system()
    url = DOWNLOADS.get(system)
    if not url:
        raise ValueError(f"不支持的平台: {system}")
    exe_name = "steamcmd.exe" if system == "Windows" else "steamcmd"
    dest = args.root / "steamcmd"
    steamcmd = dest / exe_name
    if steamcmd.exists() and not args.force:
        return {"ok": False, "installed": False, "reason": "already-installed",
                "steamcmd": str(steamcmd)}
    dest.mkdir(parents=True, exist_ok=True)
    archive = args.root / ("steamcmd.zip" if system == "Windows" else "steamcmd.tar.gz")
    with urllib.request.urlopen(url) as resp:
        archive.write_bytes(resp.read())
    if system == "Windows":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                tf.extractall(dest)
        steamcmd.chmod(0o755)
    archive.unlink()
    output = run_steamcmd({"steamcmd": steamcmd, "steamhome": args.root / "steamhome"}, ["+quit"], 300)
    bootstrap_ok = "Steam Console Client" in output
    return {"ok": bootstrap_ok, "installed": True, "steamcmd": str(steamcmd),
            "steamhome": str(args.root / "steamhome"),
            "outputTail": tail(output), "hint": "自举失败时核对 Rosetta 2 与下载地址（见 steam-env.md）"}


def cmd_login_check(args):
    config = load_config(args.config)
    if not config["steamcmd"].exists():
        return {"ok": False, "reason": "steamcmd-missing", "steamcmd": str(config["steamcmd"])}
    try:
        output = run_steamcmd(config, ["+login", config["account"], "+quit"], args.timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "relogin",
                "detail": "非交互运行超时，通常为令牌失效后停在密码提示；请用户在终端重新交互登录"}
    reason = classify_login(output)
    result = {"ok": reason == "ok", "outputTail": tail(output)}
    if reason != "ok":
        result["reason"] = reason
    return result


def cmd_vdf(args):
    check = preflight(args.staging, args.project, args.preview)
    if not check["passed"]:
        return {"ok": False, "issues": check["issues"]}
    out = args.out
    if out is None and args.project is not None:
        out = args.project / ".local" / "workshop-upload.vdf"
    if out is None:
        raise ValueError("未提供 --project 或 --out，无法确定 VDF 输出位置")
    out = Path(out)
    if out.resolve().is_relative_to(Path(args.staging).resolve()):
        raise ValueError("VDF 不能写入上传内容目录（contentfolder）内")
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        ("appid", APPID),
        ("publishedfileid", check["publishedFileId"] or "0"),
        ("contentfolder", str(Path(args.staging).resolve())),
        ("previewfile", str(Path(check["preview"]["path"]).resolve())),
        ("visibility", str(args.visibility)),
        ("title", args.title or check["modName"]),
        ("description", args.description or ""),
        ("changenote", args.changenote or ""),
    ]
    lines = ['"workshopitem"', "{"]
    lines += [f'\t"{key}"\t\t"{vdf_text(str(value)) if key in ("description", "changenote") else escape_vdf(str(value))}"'
              for key, value in fields]
    lines.append("}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "vdf": str(out), "mode": check["mode"],
            "title": args.title or check["modName"], "visibility": args.visibility,
            "publishedfileid": check["publishedFileId"] or "0"}


def cmd_upload(args):
    config = load_config(args.config)
    vdf = Path(args.vdf)
    if not vdf.is_file():
        raise ValueError(f"VDF 不存在: {vdf}")
    before = read_vdf_id(vdf)
    try:
        output = run_steamcmd(
            config,
            ["+login", config["account"], "+workshop_build_item", str(vdf), "+quit"],
            args.timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout",
                "detail": "上传超时：大内容请加大 --timeout 或转后台运行"}
    ok = "success" in output.lower()
    result = {"ok": ok, "outputTail": tail(output, 1200)}
    if ok:
        after = read_vdf_id(vdf)
        result["mode"] = "update" if before not in (None, "0") else "create"
        item_id = after if before in (None, "0") and after not in (None, "0") else before
        if item_id:
            result["publishedFileId"] = item_id
            result["workshopUrl"] = WORKSHOP_URL.format(item_id)
        if result["mode"] == "create" and item_id:
            result["nextStep"] = f"运行 adopt --project <工程根> --id {item_id} 回写 About/PublishedFileId.txt"
    else:
        login = classify_login(output)
        result["reason"] = "relogin" if login == "relogin" else "upload-failed"
        result["detail"] = "令牌失效请用户重新交互登录；其余错误见 upload.md 错误对照"
    return result


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
    pre = sub.add_parser("preflight", help="核对发行产物并判定创建/更新模式，无需 Steam 环境")
    pre.add_argument("--staging", required=True, type=Path)
    pre.add_argument("--project", type=Path)
    pre.add_argument("--preview", type=Path)
    ins = sub.add_parser("install", help="下载 steamcmd 到专用目录并自举，需用户安装授权")
    ins.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ins.add_argument("--force", action="store_true")
    log = sub.add_parser("login-check", help="免密登录令牌健康检查")
    log.add_argument("--config", required=True, type=Path)
    log.add_argument("--timeout", type=int, default=90)
    vdfp = sub.add_parser("vdf", help="生成本机 .local/workshop-upload.vdf")
    vdfp.add_argument("--staging", required=True, type=Path)
    vdfp.add_argument("--project", type=Path)
    vdfp.add_argument("--preview", type=Path)
    vdfp.add_argument("--out", type=Path)
    vdfp.add_argument("--title")
    vdfp.add_argument("--description")
    vdfp.add_argument("--changenote")
    vdfp.add_argument("--visibility", type=int, choices=(0, 1, 2), default=2)
    up = sub.add_parser("upload", help="隔离运行 steamcmd 上传，需用户上传授权")
    up.add_argument("--config", required=True, type=Path)
    up.add_argument("--vdf", required=True, type=Path)
    up.add_argument("--timeout", type=int, default=600)
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
        "install": (cmd_install, "ok"),
        "login-check": (cmd_login_check, "ok"),
        "vdf": (cmd_vdf, "ok"),
        "upload": (cmd_upload, "ok"),
        "adopt": (cmd_adopt, "adopted"),
    }
    try:
        func, ok_key = handlers[args.command]
        result = func(args)
        emit(result)
        return 0 if result.get(ok_key) else 1
    except (OSError, ValueError, KeyError, ET.ParseError) as ex:
        emit({"error": str(ex)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

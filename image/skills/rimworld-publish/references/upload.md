# 工坊上传

上传源是 rimworld-release 生成的 staging 目录。SDK 的 `SetItemContent` 接收 mod 文件夹本身的绝对路径，其**内容**成为物品根（RimWorld 工坊物品要求 `About/` 在物品根）；staging 可以是"恰含一个 mod 文件夹"的父目录，也可以直接就是 mod 文件夹，preflight 两种都接受。预检（`preflight`）通过才继续。

## 执行流程

`publish.py upload` 以 Steamworks SDK 直连桌面客户端会话（见 [sdk-env.md](sdk-env.md)），一次上传分多笔独立提交（每笔独立校验结果，全部成功才算 ok）：

1. **主提交**：CreateItem（新模式）或既有 ID 的 update，设标题（默认 About.xml `<name>`）、描述、主预览图（`About/Preview.png|.jpg`，≥512×512 且 <1MB，工坊按内容识别格式）、内容目录、可见性（0 公开 / 1 好友 / 2 私密）、变更说明。
2. **语言槽**：工程 `public/` 存在时，每个 `<语言码>.txt` 单独一笔提交写入对应语言槽（`SetItemUpdateLanguage`）。工坊按访客客户端语言显示对应槽，缺翻译回退默认（英文）槽。`public/english.txt` 存在时其描述优先于 `--description` 参数。
3. **截图库**：`public/Screenshots/` 非空时，先删除线上全部附加截图再按文件名顺序追加（快照式替换）；本地为空且线上已有截图时跳过，防误清空。上限 20 张。

## public/ 页面素材约定

```
<工程根>/public/
├── english.txt      ← 各语言槽：首行标题，空行后为 BBCode 描述
├── schinese.txt
└── Screenshots/
    └── *.png|jpg    ← 按文件名排序上传
```

`public/` 不进发行包（rimworld-release 打包排除），玩家下载的 mod 内容不包含它；它只在 publish 时作为 SDK 更新工坊页面的素材。页面维护与内容发布解耦：只改页面用 `page --project`，不动 staging。

## 创建与更新

- **创建**：preflight 判定 `create`（无 PublishedFileId）时 upload 先 CreateItem 拿新 ID，成功后用 `adopt` 把 ID 写回工程 `About/PublishedFileId.txt`。RimWorld 生态以该文件为更新凭据，丢失会把下次上传变成重复创建。
- **更新**：有 ID 即为更新，Steam 以 ID 识别物品，保持同一 ID 才是更新。
- **首次创建两段式**：默认 visibility 2（私密）上传 → 用户打开工坊页检查并接受 Steam Workshop 法律协议（首次发布必须，否则物品保持隐藏）→ 改 visibility 0 再传一次转公开。返回的 `needsAccept`/`legalAgreement` 字段会提示该步骤。

## 结果验证

`upload`/`page` 成功后运行 `inspect --id <ID> --language <语言码>` 按语言读回标题、描述、可见性、主图与截图数，作为页面级自验证交付给用户；游戏内实际加载效果仍属游戏验收，不由上传成功代替。

## 错误对照

| 现象 | 处理 |
| --- | --- |
| `not-logged-on`（session/upload/page/inspect） | 桌面客户端未运行或未登录：请用户打开客户端登录后重试；代理不代输密码 |
| `SteamAPI_InitFlat 失败` | 客户端未运行、或 Steamworks 库与客户端会话不匹配：确认客户端在线后重试 |
| 未找到 Steamworks 库（preflight issue / `--dylib` 报错） | RimWorld 未通过 Steam 安装或路径特殊：确认安装，或设 `RIMWORLD_STEAM_DYLIB` / 传 `--dylib`（见 sdk-env.md） |
| `SubmitItemUpdate 失败: 25(LimitExceeded)` | 预览图超 1MB 硬上限：压缩/降分辨率后重传；preflight 已前置检查 |
| `SubmitItemUpdate 失败: 2(Fail)` | Steam 端偶发通用失败（实测连续多笔提交后出现过一次）：脚本已自动重试一次，仍失败则整笔重跑 `upload`/`page`（每笔独立生效，无半提交状态） |
| `SubmitItemUpdate 失败: 15(AccessDenied)` 等 | 核对登录账号拥有 RimWorld、物品属于该账号、`About/PublishedFileId.txt` 的 ID 正确 |
| `等待结果超时` | 大内容上传耗时：加大 `--timeout` 重试一次；仍超时先 `inspect` 确认是否实际已提交，不盲目重传 |
| 更新成功但工坊页暂未显示新内容 | 等待 Steam 索引刷新后用 `inspect` 复查，不立即连续重复上传 |

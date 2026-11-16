# 工坊上传

上传源是 rimworld-release 生成的 staging 目录：steamcmd 把 `contentfolder` 整个递归上传、不挑文件，所以它只能指向发行产物目录，其下应恰有一个 mod 文件夹（内含 `About/`）。预检（`preflight`）通过才继续。

## VDF 字段（RimWorld 取值）

| 字段 | 取值 |
| --- | --- |
| appid | `294100` |
| publishedfileid | 创建留 `"0"`；更新填 `About/PublishedFileId.txt` 的值 |
| contentfolder | staging 目录绝对路径 |
| previewfile | 预览图绝对路径，最低 512×512 PNG/JPG（RimWorld 惯例 512×512，常放 `About/Preview.png`） |
| visibility | 0 公开 / 1 好友 / 2 私密 |
| title / description / changenote | About 名称 / 工坊描述（换行写 `\n`）/ 本次变更说明 |

`scripts/publish.py vdf` 从预检结果与参数生成 `.local/workshop-upload.vdf`。VDF 只放 `.local/`，绝不放进 contentfolder：创建成功后 steamcmd 会把新物品 ID 写回该文件，它属于本机状态，不应被再次上传。

## 创建与更新

- **创建**：publishedfileid 留 `"0"`。成功后 steamcmd 把新 ID 写回 VDF；用 `publish.py adopt` 把 ID 写入工程源码 `About/PublishedFileId.txt`，让后续发行自带。RimWorld 生态以该文件为更新凭据，丢失会把下次上传变成重复创建。
- **更新**：VDF 填已有 ID 重跑上传；Steam 以 ID 识别物品，保持同一 ID 才是更新。
- **首次创建建议两段式**：visibility 2（私密）上传 → 用户打开工坊页检查并接受 Steam Workshop 法律协议（首次发布必须，否则物品保持隐藏）→ 改 visibility 0 再传一次转公开。

## 执行与结果

`publish.py upload` 以 `HOME=<steamhome>`（macOS arm64 加 `arch -x86_64`）运行：

```text
steamcmd +login <账号名> +workshop_build_item <vdf> +quit
```

- 上传大 mod 可能远超常规命令超时：给足长超时或转后台运行并轮询，不用短超时误判失败。
- 成功输出含 `Success`；失败先查错误对照，不盲目重试。
- 成功后交付工坊链接 `https://steamcommunity.com/sharedfiles/filedetails/?id=<ID>`；创建模式补 `adopt` 回写。游戏内实际加载效果仍属游戏验收，不由上传成功代替。

## 错误对照

| 输出特征 | 处理 |
| --- | --- |
| `Cached credentials not found`，或非交互运行下停在密码提示/超时挂起 | 令牌失效：请用户在终端重新交互登录（见 [steam-env.md](steam-env.md)），代理不代输密码 |
| `ERROR!` 含 timeout / 连接类字样 | 瞬时网络问题：原样重试一次，再失败如实报告 |
| `Failed to init session manager` 等会话类错误 | 核对 steamhome 隔离配置；同一专用目录同时只能跑一个 steamcmd，等待现有进程结束 |
| 登录层通过但上传报权限 / No subscription | 核对账号持有 RimWorld、VDF appid 为 294100、物品属于该账号 |
| 更新成功但工坊页暂未显示新内容 | 等待 Steam 索引刷新后复查，不立即连续重复上传 |

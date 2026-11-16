---
name: rimworld-publish
description: 把 rimworld-release 核对过的发行包上传或更新到 Steam 创意工坊；steamcmd 安装与登录仅在实际上传时按需进行，Steam 密码与令牌不经过代理，上传需用户明确授权。
---

# RimWorld 工坊发布

仅当用户明确要求上传或更新创意工坊时进入本技能。上传源只接受 [发行准备](../rimworld-release/SKILL.md) 生成的干净 staging 目录及其清单；没有产物先回发行准备，不从工程源码目录直接上传。

环境与上传要点见 [steam-env.md](references/steam-env.md) 与 [upload.md](references/upload.md)。steamcmd 安装和登录都是按需动作：预检不需要任何 Steam 环境，未取得上传决定前不安装 steamcmd、不提示登录；环境齐备时直接复用。

1. 运行 `scripts/publish.py preflight --staging <发行产物目录>` 核对 staging 结构、About.xml、预览图，并按 `About/PublishedFileId.txt` 判定创建或更新模式。
2. 预检通过且用户确定上传后，才按 [Steam 环境准备](references/steam-env.md) 检查环境：缺 steamcmd 就地安装（需安装授权），令牌缺失或过期时请用户在自己终端交互登录；同一环境重复使用，不重复安装。
3. 汇总本次上传的标题、可见性、内容摘要与变更说明，取得用户对本次上传的明确授权。首次创建默认私密可见，用户在工坊页检查并接受法律协议后再转公开。
4. 运行 `scripts/publish.py vdf` 生成 `.local/workshop-upload.vdf`，再运行 `upload` 以隔离 HOME 执行 steamcmd 上传并解析结果。
5. 创建成功后把新物品 ID 回写工程源码 `About/PublishedFileId.txt`（`adopt` 子命令），交付工坊链接；更新成功后核对变更说明已生效。

代理不接触 Steam 密码，令牌只存在于本机专用目录的 steamcmd 配置中，不进发行包或仓库。VDF 只放 `.local/`，绝不放进上传内容目录；创建成功后 steamcmd 会把新 ID 写回 VDF，该文件属于本机状态。同一专用目录不并发运行多个 steamcmd。失败按 [upload.md](references/upload.md) 的错误对照处理：令牌过期只提示用户重新交互登录，不代输密码。

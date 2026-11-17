---
name: rimworld-publish
description: 把 rimworld-release 核对过的发行包上传或更新到 Steam 创意工坊（含多语言标题描述与截图库）；经 RimWorld 自带的 Steamworks SDK 直连桌面客户端登录会话，无令牌无密码，上传需用户明确授权。
---

# RimWorld 工坊发布

仅当用户明确要求上传或更新创意工坊时进入本技能。上传源只接受 [发行准备](../rimworld-release/SKILL.md) 生成的干净 staging 目录及其清单；没有产物先回发行准备，不从工程源码目录直接上传。

环境要求与上传要点见 [sdk-env.md](references/sdk-env.md) 与 [upload.md](references/upload.md)。本技能不安装任何东西、不管理任何凭据：它直接加载本机 RimWorld 自带的 Steamworks 库，复用桌面客户端的登录会话，因此 Steam 密码与令牌都不经过代理。

1. 运行 `scripts/publish.py preflight --staging <发行产物目录> --project <工程根>` 核对 staging 结构、About.xml、预览图、Steamworks 库与 `public/` 页面素材，并按 `About/PublishedFileId.txt` 判定创建或更新模式。预检不需要客户端在线。
2. 预检通过且用户确定上传后，运行 `session` 确认桌面客户端已登录；未登录就请用户打开客户端登录后重试，不代输密码。
3. 汇总本次上传的标题、可见性、内容摘要、语言槽与截图清单，取得用户对本次上传的明确授权。首次创建默认私密可见，用户在工坊页检查并接受法律协议后再转公开。
4. 运行 `scripts/publish.py upload --staging <目录> --project <工程根>`：一次完成内容+主图+英文槽+可见性+变更说明，若工程有 `public/` 则一并写入各语言槽与截图库。创建成功后用 `adopt` 把新物品 ID 回写 `About/PublishedFileId.txt`。
5. 运行 `scripts/publish.py inspect --id <ID> --language <语言码>` 按语言读回标题/描述/截图数，向用户交付工坊链接与读回结果；游戏内实际加载效果仍属游戏验收。

页面维护独立于内容发布：改了 `public/` 的多语言描述或截图而不动 mod 内容时，直接用 `page --project <工程根>` 只更新工坊页面，不需要重新打包 staging。

代理不接触 Steam 密码（整个方案也没有令牌概念）。`public/` 是工坊页面素材目录，不进发行包（见发行准备的排除规则）；截图库更新以 `public/Screenshots` 为快照做替换，本地为空且线上已有截图时跳过以防误清空。失败按 [upload.md](references/upload.md) 的错误对照处理；客户端未登录只提示用户登录，不代输密码。

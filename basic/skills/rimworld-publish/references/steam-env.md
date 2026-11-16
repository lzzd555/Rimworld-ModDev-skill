# Steam 环境（按需准备）

本页所有步骤仅在用户已决定上传且环境缺失时执行，不是 rimworld-publish 的前置配置；环境齐备时直接复用，不重复安装或登录。这些准备不影响其余入口的任何前置。

## 专用目录布局

steamcmd 与登录令牌全部放在本机专用目录，与系统里其他 Steam 安装隔离：

```text
~/.rimworld-publish/
├── steamcmd/                    ← steamcmd 本体（含自更新组件）
└── steamhome/                   ← 运行 steamcmd 时的 HOME
    └── Library/Application Support/Steam/
        └── config/config.vdf    ← 登录令牌，由 steamcmd 自行写入与续期
```

macOS 与 Linux 上 steamcmd 默认把令牌写进与桌面客户端共享的目录（本仓库 2026-11 实测确认），必须以 `HOME=<专用 steamhome>` 运行才能隔离。绝不把 steamhome 指向真实用户主目录或其下的 Steam 目录；publish.py 会拒绝这种配置。该目录永不进发行包、仓库或工坊内容。

## 平台与安装

| 平台 | 下载包 | 运行方式 |
| --- | --- | --- |
| macOS arm64 | steamcmd_osx.tar.gz | 先装 Rosetta 2（`softwareupdate --install-rosetta --agree-to-license`，需管理员），运行时用 `arch -x86_64` 包裹；HOME 隔离 |
| macOS Intel | steamcmd_osx.tar.gz | 直接运行；HOME 隔离 |
| Windows | steamcmd.zip | 直接运行 steamcmd.exe；默认使用自身目录，无需 HOME 隔离 |
| Linux | steamcmd_linux.tar.gz | 直接运行；HOME 隔离（默认会写 `~/.steam`） |

下载地址于 2026-11-16 核对：`https://steamcdn-a.akamaihd.net/client/installer/` 下的 `steamcmd_osx.tar.gz` / `steamcmd.zip` / `steamcmd_linux.tar.gz`。实际安装时重新核对官方文档，不依赖固定文本。

安装即在上传决定后、取得用户安装授权时运行 `scripts/publish.py install`：下载解压到 `~/.rimworld-publish/steamcmd` 并做一次 `+quit` 自举（完成自更新与令牌目录初始化），macOS arm64 的 Rosetta 缺失会在自举中暴露为明确报错。

## 用户交互登录（唯一需要密码的步骤）

令牌缺失或过期时，把对应平台的命令交给用户在其终端执行；steamcmd 会隐藏式提示输入密码，必要时再提示 Steam Guard 码（邮箱码或手机 App 码）。代理不代输、不转述密码。

```bash
HOME=~/.rimworld-publish/steamhome \
  arch -x86_64 ~/.rimworld-publish/steamcmd/steamcmd +login <Steam账号名>
```

（Intel macOS / Linux 去掉 `arch -x86_64`；Windows 直接运行 steamcmd.exe。）若 Guard 码到达时登录已超时，在 steamcmd 内先 `set_steam_guard_code <码>` 再重新 `login <账号> <密码>`。看到 `Logged in OK` 后输入 `quit`。

登录成功后令牌缓存于专用目录，之后 `+login <账号名>` 免密；每次成功登录自动续期，约 30 天不使用才过期，用户修改 Steam 密码后作废。频繁发布者基本不会再遇到登录提示。

## 三层验证

1. **安装层**：steamcmd 能启动并完成自举（版本横幅可见）。
2. **登录层**：`scripts/publish.py login-check` 报告免密登录 OK；提示密码或 `Cached credentials not found` 即令牌失效，回上一节请用户重新交互登录。
3. **上传层**：`workshop_build_item` 实际执行成功。前两层通过不证明上传必然成功，失败按 [upload.md](upload.md) 错误对照处理。

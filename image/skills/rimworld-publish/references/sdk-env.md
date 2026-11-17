# SDK 环境

本技能的 Steam 环境零安装、零凭据：直接加载本机 RimWorld 自带的 Steamworks 库（`libsteam_api`），通过本机 IPC 复用桌面客户端的登录会话。没有 steamcmd、没有令牌文件、没有账号配置——客户端登录着就能用，所有检查按需进行，不做前置准备。

## 依赖三项

1. **桌面 Steam 客户端正在运行且已登录**（工具运行期间需保持在线；`session` 子命令随时可查）。
2. **通过 Steam 安装的 RimWorld**（提供 Steamworks 库与 294100 的 app 上下文；账号需拥有 RimWorld）。
3. 库路径自动探测，找不到时设环境变量 `RIMWORLD_STEAM_DYLIB` 或传 `--dylib`：

探测顺序（2026-11 实测补充）：先取各平台默认 Steam 安装根——Windows 优先读注册表
`HKCU/HKLM:\Software\Valve\Steam\SteamPath`（主安装可在任意盘，实测 d:/steam），
再加 `Program Files (x86)`/`Program Files` 下的常规位置；随后解析每个根的
`steamapps/libraryfolders.vdf` 收齐所有附加库（自定义盘符库也在内）；最后在每个库的
`steamapps/common/RimWorld/` 下按平台找库文件：

| 平台 | 库文件相对路径 |
| --- | --- |
| macOS | `RimWorld.app/Contents/MacOS/libsteam_api.dylib` |
| Windows | `steam_api64.dll`（及 `RimWorldWin64/steam_api64.dll` 旧布局） |
| Linux | `libsteam_api.so` |

macOS 的库是 universal binary（x86_64 + arm64），arm64 进程直接加载，无需 Rosetta。库与调用方按架构匹配即可，API 三平台一致。

## 运行目录

首次调用时在 `~/.rimworld-publish/steamworks/` 写入 `steam_appid.txt`（内容 294100）并在调用期间 chdir 到该目录、设置 `SteamAppId`/`SteamGameId`/`SteamOverlayGameId` 环境变量——这是 Steamworks 以 RimWorld 身份初始化的要求；所有传给 SDK 的路径会先绝对化，不受 chdir 影响。该目录只有这一个常量文件，无任何凭据。

## 已知行为要点（2026-11 实测）

- 初始化必须走 `SteamAPI_InitFlat`：无参的 `SteamInternal_SteamAPI_Init` 会误报失败（版本校验），不代表客户端有问题。
- 回调结构体实际为 pack4 内存布局（非头文件声明的 pack8），publish.py 已按实测偏移解析，返回的物品 ID 会做量级合理性交叉校验。
- headless（ManualDispatch）模式轮询回调，不依赖任何游戏循环；单个进程同一时间只开一个会话。
- 本方案无法 headless/CI 无人值守（依赖桌面客户端登录会话）；无人值守场景不在本技能范围内。

## 失败对照

| 现象 | 处理 |
| --- | --- |
| 客户端进程不在 | `open -a Steam`（macOS）启动并登录后重试；代理不代输密码 |
| InitFlat 失败但客户端在运行 | 多为客户端正在更新或刚启动未就绪：稍等重试一次 |
| 库加载失败（OSError/dlopen） | 确认 RimWorld 完整安装（验证游戏文件），或显式指定 `--dylib` |
| 库架构不匹配 | 库为 universal 一般无此问题；自定义路径时确认与 Python 进程架构一致 |

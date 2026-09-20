# ThinkPad T14s Gen 4 FanControl

针对 **ThinkPad T14s Gen 4（ES 工程 CPU + 工程 BIOS）** 的智能风扇控制工具。
解决该机型工程固件下 **BIOS/EC 风扇曲线失效、风扇不转** 的问题。

基于 [TPFanCtrl2](https://github.com/Shuzhengz/TPFanCtrl2) 的核心思路用 Python 重写：
去掉多余功能，修复其"配置不持久化"的缺陷，直连 PawnIO 访问 EC。

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey) ![License](https://img.shields.io/badge/License-MIT-green)

## 功能

- **托盘风扇图标**：颜色即状态（绿=智能 / 橙=手动 / 红=最强散热 / 蓝=EC自动 / 灰=EC 通信失败）
- **悬停显示**：当前模式 / CPU 温度 / 风扇转速
- **四种模式**（托盘右键切换，立即生效并持久化）：
  | 模式 | 行为 |
  |---|---|
  | 智能模式 | 曲线自动调速，带 3°C 降档滞回 |
  | 手动模式 | 固定档位 0(停转)/1~7/64(满速)，重启恢复 |
  | 最强散热 | 固定 0x40 真满速 ≈4950 RPM |
  | EC 自动(备用) | 写 0x80 交还固件（供固件修复后使用） |
- **失效保护**：CPU ≥88°C 时任何模式强制拉满速，降温后自动恢复
- **开机自启**：计划任务方式（最高权限，登录 15 秒后静默启动）
- **退出保持档位**：退出不会把风扇交还给坏的 EC 曲线
- **日志自动轮转**：3MB 上限，正常时仅换档才写日志

## 背景：这台机器的风扇为什么不转

该工程机 BIOS 按 CPUID 匹配风扇曲线失败，回退到一张坏的 EC 曲线 —— 结果是
**EC 自动模式下风扇几乎不转**。本工具绕过固件，直接通过 PawnIO 读写 EC 寄存器
（HFSP=0x2F，0x80=EC自动 / 0x40=真满速，档位 0x00~0x07）。

实测档位表（T14s Gen 4 工程机）：

| HFSP | 0x00 | 0x01 | 0x02 | 0x03 | 0x04 | 0x05 | 0x06 | 0x07 | 0x40 |
|---|---|---|---|---|---|---|---|---|---|
| 转速 | 0 | 2029 | 2362 | 2600 | 2964 | 3185 | 3170 | 3924 | **4946** |

> 注意 0x40 才是真满速，比默认曲线天花板 0x07 高约 1000 RPM。

## 使用

1. 安装 [PawnIO](https://pawnio.com/) 驱动
2. 下载 Release 中的 `FanControl.exe`（或从源码构建），双击运行（自动请求管理员权限）
3. 托盘右键选模式即可，配置自动保存在 exe 同目录 `config.json`

开机自启：双击 `scripts/install_autostart.bat`（取消用 `remove_autostart.bat`）。

## 从源码构建

```bat
pip install pystray pillow pyinstaller
cd src
pyinstaller --onefile --noconsole --uac-admin --name FanControl --icon fan.ico ^
  --hidden-import=pystray._win32 --add-binary "LpcACPIEC.bin;." fan_control.py
```

或直接双击 `scripts/重新打包.bat`（需本机有 Python 环境）。

## 文件结构

```
├── src/          Python 源码 / 打包配置 / 图标 / PawnIO EC 模块
├── scripts/      开机自启注册、移除、重新打包脚本
└── docs/         详细使用说明
```

## 已知限制

- 仅适配 T14s Gen 4（EC 布局相同的机型理论上可用，未验证）
- 需要管理员权限（PawnIO 加载 EC 模块的要求）
- 不要与 TPFanCtrl2 / TPFanControl 等其他风扇软件同时运行（会争抢 EC）

## 致谢

- [Shuzhengz/TPFanCtrl2](https://github.com/Shuzhengz/TPFanCtrl2) — ImGui 重构版 ThinkPad 风扇控制
- [blackholefox/TPFanControl](https://github.com/blackholefox/TPFanControl)（TPFC 2.3.3）— 经典曲线思路
- [PawnIO](https://pawnio.com/) — 安全的端口/硬件访问驱动

## License

MIT

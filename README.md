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
  | 手动模式 | 固定档位 0(停转)/1/2/6/7/64(满速)，重启恢复 |
  | 最强散热 | 固定 0x40 真满速 ≈4810 RPM |
  | EC 自动(备用) | 写 0x80 交还固件（天花板只有 ≈3890，压不住温度） |
- **默认曲线**：`40°C→停 / 45°C→1档 / 50°C→2档 / 62°C→6档 / 84°C→满速`（只用实测有区分度的档）
- **失效保护**：CPU ≥88°C 时任何模式强制拉满速，降温后自动恢复
- **EC 访问节流**：5 秒一轮读温度；转速每 6 轮（约 30 秒）才读一次，避免和 CPU 工具抢总线
- **唤醒自愈**：每 30 秒核对一次 HFSP，被固件 `OWAK()` 抢回档位时自动纠正
- **开机自启**：计划任务方式（最高权限，登录 15 秒后静默启动）
- **退出保持档位**：退出不会把风扇交还给坏的 EC 曲线
- **日志自动轮转**：3MB 上限，正常时仅换档才写日志

## 背景：这台机器的风扇天花板

该工程机 BIOS 按 CPUID 匹配风扇曲线失败，回退到一张很平的 EC 曲线 ——
实测 **原生自动（0x80）最高只到 ≈3890 RPM**，无论 54°C 还是 99°C 几乎不变，
压得住轻载、压不住满载（28W 满载会稳定在 95~99°C）。本工具绕过固件，直接通过
PawnIO 读写 EC 寄存器（HFSP=0x2F，0x80=EC自动 / 0x40=满速，档位 0x00~0x07）。

实测档位表（T14s Gen 4 工程机，2026-10-20 复测，见维护手册 `scripts/风扇档位-20261020-075530.txt`）：

| HFSP | 0x00 | 0x01 | 0x02 | 0x03 | 0x04 | 0x05 | 0x06 | 0x07 | 0x40 |
|---|---|---|---|---|---|---|---|---|---|
| 转速 | 0 | 2471 | 2858 | 3181 | 3186 | 3167 | 3456~3861 | 3858 | **4807** |

> ⚠️ **别抄旧表**：以前 README 里那张（2029/2362/2600/2964/3185/3170/3924/4946）是
> 从别的机型搬来的，本机实测对不上（0x05 比 0x06 还高、0x06 差了 700 转）。
> 现在这张是逐个档位等 25 秒实测的中位数。
>
> - 0x03/0x04/0x05 三档都挤在 ~3180，没有区分度，界面里已去掉。
> - 0x06 会随温度变（低温 3456 / 高温 3861），0x07 稳定 3858。
> - **0x40 是唯一确定的满速**（4807），与温度/功耗无关，但要 20~25 秒才到位。
> - **原生自动（0x80）天花板只有 ≈3890**，到不了 0x40 —— 所以"交还 EC"压不住温度。
>
> **不写 0x31**：老 IBM/X60 那代才用它选主/副风扇；本机 0x31 是 HUWB/VPON/VRST，
> 写 0 会清掉 VPON/VRST 位，而实测写与不写转速完全一样。

## 使用

1. 安装 [PawnIO](https://pawnio.com/) 驱动（本目录的 `1-启动风扇控制.bat` 会先检查）
2. 双击 `1-启动风扇控制.bat`（自动请求管理员权限，托盘出现风扇图标）
3. 右键图标选模式即可，配置自动保存在本目录 `config.json`

开机自启：托盘右键 →「开机自启」（计划任务 `FanControl-AutoStart`，登录 15 秒后启动）。

停止：托盘右键 →「退出（保持当前状态）」，或双击 `2-停止风扇控制.bat`。

> 本工具只管风扇（EC 0x2F/0x78/0x84/0x85）。
> CPU 功耗墙在 `..\cpu\`（CpuPLFix），两者互不写对方的寄存器。

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

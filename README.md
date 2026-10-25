# T14scontrol

ThinkPad **T14s Gen 4（ES 工程机）** 的常驻控制工具集：两个 SYSTEM 服务 + 一个任务栏显示 + 一次性迁移脚本。

> **三套代码互不写对方的寄存器**，只共用一条 EC 总线（`0x66/0x62`）和一把跨进程锁 `Global\CpuPLFix-EC`。
> 完整手册（机器档案 / EC 寄存器全表 / 参数速查 / 排障）在另一个仓库：
> [thinkpad-t14s-gen4-maintenance](https://github.com/HalfBilt/thinkpad-t14s-gen4-maintenance)

---

## 结构

```
%APPDATA%\T14scontrol\
├── runtime\        共用 Python 3.13（两个服务都跑它，别再各带一份）
├── cpu\            CpuPLFix   —— 功耗墙 + 过热保护（MSR/MMIO）
│   ├── CpuPLFix.py         主程序
│   ├── CpuPLFixSvc.py      服务入口
│   ├── CpuPLFixCfg.py      配置工具
│   ├── TSCtl.sys           内核驱动（端口 IO / MSR / MMIO）
│   ├── dev\                离线回归 selftest.py / EC 批量扫描 ec_scan.py
│   └── ec-fix\             2026-10-23 驱动迁移工具（一次性，留档）
├── fan\            FanControl —— 风扇（EC 0x2F 档位）
│   └── src\                fan_control.py / FanControlSvc.py / fan_service_ctl.py
├── hud\            任务栏显示（只读 status\*.json，不碰 EC/MSR，免管理员）
├── logs\           运维脚本的日志
└── status\         两个服务每拍写的快照 cpu.json / fan.json
```

## 三个组件

| 目录 | 管什么 | 服务名 | 碰什么 |
|---|---|---|---|
| `cpu\` | 功耗墙 + 过热保护 | `CpuPLFixSvc` | MSR `0x610`、MCHBAR+`0x59A0`；EC `0x89~8B` 只读 |
| `fan\` | 风扇档位 | `T14sFanSvc` | EC `0x2F` 写、`0x84/85` 读 |
| `hud\` | 任务栏数值显示 | 无（用户态进程 + 计划任务 `T14sHUDGuard`） | 只读 `status\*.json` |

**温度读取**：两个服务都优先走 **ACPI 热区**（PDH 计数器 `\_TZ.THM0`），对 EC 总线零占用；
拿不到才退回裸读 EC `0x78`。启动日志会明确打出温度来源。

## 安装 / 恢复

都要管理员，各装各的、互不依赖：

1. CPU 侧：双击 `cpu\1-install.bat`
2. 风扇侧：双击 `fan\1-安装风扇服务.bat`
3. 任务栏：双击 `hud\5-任务栏显示-开关.bat`（免管理员）

两个 install 脚本都会重写服务路径，**换机器 / 挪目录后重跑一次即可**。

> `runtime\` 不在仓库里（60MB Python 运行时）。新机器先把它放到 `%APPDATA%\T14scontrol\runtime\`，
> 或让 `cpu\1-install.bat` 从 `runtime.zip` 解压。

## 日常

| 要做什么 | 命令 |
|---|---|
| CPU 体检（需管理员） | `cpu\2-check.bat` |
| 改功耗墙 / 过热保护参数 | `cpu\3-config.bat`（改完 1 分钟内生效） |
| 立即对齐一次功耗墙 | `cpu\3-fix-now.bat` |
| 改了 CPU 代码后重启 | `cpu\部署并重启.bat` |
| 风扇状态 + 最近日志 | `fan\3-风扇服务状态.bat` |
| 改风扇配置后重启 | `fan\4-重启风扇服务.bat` |
| 卸载 | `cpu\9-uninstall.bat` · `fan\2-卸载风扇服务.bat` |

日志：`cpu\logs\fix.log`、`fan\fan_service.log`。

## 要备份的

| 文件 | 说明 |
|---|---|
| `cpu\config.json` | 功耗墙 / 过热保护参数（已在仓库里） |
| `fan\config.json` | 风扇曲线 / 阈值 / 驻留（**已在仓库里**，2026-10-25 起纳入跟踪） |

## 已知边界

- 风扇侧只管 EC `0x2F`/`0x84`/`0x85`；MSR（功耗墙、DTS、倍频）归 CPU 侧。
- 联想固件每 ~30 秒重写 `0x2F`，服务只能"只防变弱"守望，无法真正接管。
- 原生自动（`0x80`）天花板 ≈3890 RPM，别指望"交还 EC"能压住温度。
- **不要**和 TPFanCtrl2 / ThrottleStop / HWiNFO 同时跑（它们会装自己的内核驱动抢 EC/MSR）。
- 冷启动时 ACPI 会记 5 条事件 `15`（首笔裸 EC 事务与 ACPI 的固有摩擦），运行期为 0，无害。
- 内核驱动从 `C:\Windows\System32\drivers\TSCtl.sys` 加载；服务登记路径每次启动自动校正。

## 仓库历史

2026-10-25 由三个独立仓库合并而成（用 `git subtree` 保留了完整历史，`git blame` 仍能指回原提交）：

| 原仓库 | 现在的位置 |
|---|---|
| [CpuPLFix](https://github.com/HalfBilt/CpuPLFix) | `cpu/` |
| [ThinkPad-T14s-Gen4-FanControl](https://github.com/HalfBilt/ThinkPad-T14s-Gen4-FanControl) | `fan/` |
| [T14scontrol-HUD](https://github.com/HalfBilt/T14scontrol-HUD) | `hud/` |

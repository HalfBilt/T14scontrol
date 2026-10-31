# T14scontrol

ThinkPad **T14s Gen 4（ES 工程机）** 的常驻控制工具集：两个 SYSTEM 服务 + 一个任务栏显示。

> **三套代码互不写对方的寄存器**，只共用一条 EC 总线（`0x66/0x62`）和一把跨进程锁 `Global\CpuPLFix-EC`。
> 完整手册（机器档案 / EC 寄存器全表 / 参数速查 / 排障）在另一个仓库：
> [thinkpad-t14s-gen4-maintenance](https://github.com/HalfBilt/thinkpad-t14s-gen4-maintenance)

---

## 结构

```
%APPDATA%\T14scontrol\
├── runtime\        共用 Python 3.13（三个组件都跑它，别再各带一份）
├── cpu\            CpuPLFix   —— 功耗墙 + 过热保护（MSR/MMIO）
│   ├── CpuPLFix.py         主程序
│   ├── CpuPLFixSvc.py      服务入口
│   ├── CpuPLFixCfg.py      配置工具
│   ├── config.json         功耗墙 / 过热保护参数
│   ├── logs\               fix.log、boot_last.txt
│   ├── dev\                离线回归 selftest.py / EC 扫描 ec_scan.py
│   └── 1~4 号 bat          安装 / 查看状态 / 重启服务 / 卸载
├── fan\            FanControl —— 风扇（EC 0x2F 档位）
│   ├── src\                fan_control.py / FanControlSvc.py / fan_service_ctl.py
│   ├── config.json         风扇曲线 / 阈值 / 驻留
│   ├── logs\               fan_service.log、fan_control.log
│   └── 1~4 号 bat          安装 / 卸载 / 状态 / 重启
├── hud\            任务栏显示（只读 status\*.json，不碰 EC/MSR，免管理员）
│   ├── hud.py / hud.json
│   ├── logs\               toggle.log（显隐记录）、error.log（异常）、hud_debug.log（调试）
│   └── 5~7 号 bat          开关 / 关闭 / 解锁并显示
├── status\         两个服务每拍写的快照 cpu.json / fan.json（HUD 读它）
├── _archive\       历史留档：旧脚本备份、一次性迁移工具（不参与运行）
└── README.md
```

## 三个组件

| 目录 | 管什么 | 服务名 | 碰什么 |
|---|---|---|---|
| `cpu\` | 功耗墙 + 过热保护 | `CpuPLFixSvc` | MSR `0x610`、MCHBAR+`0x59A0`；EC `0x89~8B` 只读 |
| `fan\` | 风扇档位 | `T14sFanSvc` | EC `0x2F` 写、`0x84/85` 读 |
| `hud\` | 任务栏数值显示 | 无（用户态 + 计划任务 `T14sHUDGuard`） | 只读 `status\*.json` |

**温度读取**：两个服务都优先走 **ACPI 热区**（PDH 计数器 `\_TZ.THM0`），对 EC 总线零占用；
拿不到才退回裸读 EC `0x78`。启动日志会明确打出温度来源。

## 日志约定

**每个模块的日志都在自己目录下的 `logs\`**，互不混放：

| 文件 | 内容 |
|---|---|
| `cpu\logs\fix.log` | 功耗墙修复、EC 总线体检、开机/唤醒动作 |
| `fan\logs\fan_service.log` | 风扇档位决策（每次变档一行） |
| `fan\logs\fan_control.log` | 托盘模式日志（服务模式下不写） |
| `hud\logs\toggle.log` | HUD 显隐切换记录 |
| `hud\logs\error.log` | HUD 崩溃堆栈（pythonw 无控制台，靠它查） |
| `hud\logs\hud_debug.log` | 布局调试（仅当 `hud\debug.flag` 存在时生成） |

日志目录由代码自动创建，删掉也会重建。

## 脚本

所有 bat 统一：**UTF-8 无 BOM + `chcp 65001` + `net session` 提权 + 路径三级兜底**
（`%~dp0..` → `%APPDATA%` → 硬编码），不依赖 `%~dp0` 是否解析成功。

### cpu\（需要管理员）

| 脚本 | 作用 |
|---|---|
| `1-安装.bat` | 装驱动 + 服务 + 计划任务，并立即应用一次 28W 功耗墙 |
| `2-查看状态.bat` | 读真实 MSR/MMIO 功耗墙、温度、频率（只读） |
| `3-重启服务.bat` | 清 `__pycache__` + 重启 `CpuPLFixSvc` + 打印日志尾部 |
| `4-卸载.bat` | 删服务、任务、驱动 |

### fan\（1/2/4 需要管理员）

| 脚本 | 作用 |
|---|---|
| `1-安装风扇服务.bat` | 注册并启动 `T14sFanSvc` |
| `2-卸载风扇服务.bat` | 停止并删除服务 |
| `3-风扇服务状态.bat` | 服务状态 + 最近 25 行日志（只读） |
| `4-重启风扇服务.bat` | 停 → 等 2 秒 → 起 → 查状态 |

### hud\（全部免管理员）

| 脚本 | 作用 |
|---|---|
| `5-任务栏显示-开关.bat` | 没运行则启动，已运行则切换显隐 |
| `6-任务栏显示-关闭.bat` | 结束 HUD 进程 |
| `7-解锁并显示.bat` | 关掉"点击穿透"和"锁定拖动"，强制显示 |

## 安装 / 恢复

各装各的、互不依赖：

1. CPU 侧：双击 `cpu\1-安装.bat`（会弹 UAC）
2. 风扇侧：双击 `fan\1-安装风扇服务.bat`
3. 任务栏：双击 `hud\5-任务栏显示-开关.bat`

> `runtime\` 不在仓库里（60MB Python 运行时）。新机器先把它放到 `%APPDATA%\T14scontrol\runtime\`。

## 要备份的

| 文件 | 说明 |
|---|---|
| `cpu\config.json` | 功耗墙 / 过热保护参数（已在仓库里） |
| `fan\config.json` | 风扇曲线 / 阈值 / 驻留（已在仓库里） |

## 已知边界

- 风扇侧只管 EC `0x2F`/`0x84`/`0x85`；MSR（功耗墙、DTS、倍频）归 CPU 侧。
- 联想固件每 ~30 秒重写 `0x2F`，服务只能"只防变弱"守望，无法真正接管。
- 原生自动（`0x80`）天花板 ≈3890 RPM，别指望"交还 EC"能压住温度。
- **不要**和 TPFanCtrl2 / ThrottleStop / HWiNFO 同时跑（它们会装自己的内核驱动抢 EC/MSR）。
- 冷启动时 ACPI 会记 5 条事件 `15`（首笔裸 EC 事务与 ACPI 的固有摩擦），运行期为 0，无害。
- 内核驱动从 `C:\Windows\System32\drivers\TSCtl.sys` 加载；服务登记路径每次启动自动校正。
- **卸载风扇服务时**，若进程 20 秒内没停干净，脚本会拒绝删除并提示重试——
  强删会留下"标记删除"状态（`DeleteFlag=1`，StartType 变 Disabled，报 1058），只能重启清理。

## 仓库历史

2026-10-25 由三个独立仓库合并而成（`git subtree` 保留完整历史，`git blame` 仍能指回原提交）：

| 原仓库 | 现在的位置 |
|---|---|
| [CpuPLFix](https://github.com/HalfBilt/CpuPLFix) | `cpu/` |
| [ThinkPad-T14s-Gen4-FanControl](https://github.com/HalfBilt/ThinkPad-T14s-Gen4-FanControl) | `fan/` |
| [T14scontrol-HUD](https://github.com/HalfBilt/T14scontrol-HUD) | `hud/` |

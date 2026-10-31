# T14scontrol HUD

任务栏上的实时硬件读数（Windows 11 风格），配合 `CpuPLFixSvc` + `T14sFanSvc` 使用。

## 它不碰硬件

两个服务每拍把读数写到 `..\status\cpu.json` / `..\status\fan.json`，
HUD 只读文件 —— 所以**不需要管理员权限**，也不会去抢 EC 总线。

风扇状态另有一个独立心跳线程每 2 秒刷新（数值不变时最多 10 秒写一次），
所以风扇服务的 EC 读取即使被总线卡住，HUD 也不会把转速/功率显示成 `----`/`--W`；
HUD 自己也在后台每秒采样 CPU 占用，不依赖窗口重绘节拍。

## 使用

| 操作 | 效果 |
|---|---|
| 左键拖动 | 移动位置（屏幕任意位置，自动记住） |
| 右键 | 勾选显示项 / 标签（完整·简短·隐藏）/ 尺寸（小·标准·大·特大）/ 外观（跟随系统·深色·浅色）/ 底框 / 开机自启 / 恢复默认位置 / 退出 |
| 托盘图标双击 | 显示-隐藏（图标是三叶风扇，颜色随温度） |

显示项：CPU 频率、CPU 占用率、EC 温度、DTS 温度、风扇转速、风扇档位、封装功率、PL1/PL2、内存、电池、保护状态、时间。
> 「EC 温度」这一格的数据自 2026-10-23 起来自 **ACPI 热区 `\_TZ.THM0`（PDH 计数器）**，与 EC `0x78` 差 1~2°C；
> 标签沿用旧名，免得和已有的肌肉记忆打架。
屏上标签与状态全部用英文（`CPU/EC/DTS/FAN/PKG/PL/BAT`、`L3/FULL/AUTO`、`OK/WARN/T1/T2/T3/FLAG`）。

标签默认「简短」：只保留容易混淆的 `CPU/MEM/BAT`，温度、风扇、功率直接显示数值，
整体宽度比完整标签窄一截；右键可切回「完整」或选「隐藏」全部不显示。

## 实现

- 纯 `ctypes`（共用运行时是 embeddable 版，没有 tkinter / Pillow）。
- 半透明圆角底 + 抗锯齿小字：逐像素 alpha 合成后走 `UpdateLayeredWindow`
  （不是 `LWA_COLORKEY`，所以小字不会有毛边）；关掉底框时自动加一层 1px 文字阴影，浅色壁纸上也能看清。
- 字体 `Segoe UI Variable Text`，字高按任务栏高度的 0.26 倍算，和系统时钟一致。

## 配置 `hud.json`

字段：`freq/cpu/temp/dts/fan/gear/pkg/pl/mem/batt/guard/time`（`guard` = 过热保护状态）。
`pkg` 是封装功耗，`pl` 是当前生效的 PL1/PL2。

```json
{
  "enabled": true,
  "fields": ["cpu", "temp", "dts", "fan", "gear", "pkg", "pl"],
  "x": 1614,
  "y": 1107,
  "scale": 0.8,
  "theme": "dark",
  "pill": true,
  "labels": "short",
  "locked": true,
  "click_through": false,
  "offset_right": 470
}
```

`hud.json` **不进仓库**（位置和缩放因机器而异）。`locked` = 锁定位置防误拖，`click_through` =
鼠标穿透（点不到它）。

## 开关

| 脚本 | 作用 |
|---|---|
| `5-任务栏显示-开关.bat` | 有则关、无则开（`--toggle`） |
| `6-任务栏显示-关闭.bat` | 关闭 |
| `7-解锁并显示.bat` | 解锁并显示（位置锁死/穿透后找不回来时用，`--cmd unlock`） |

开机自启：右键菜单勾选（写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`）。

## 日志

`hud\logs\`：`error.log`（pythonw 下的异常）、`toggle.log`（开关记录）。

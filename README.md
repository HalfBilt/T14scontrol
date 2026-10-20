# T14scontrol HUD

任务栏上的实时硬件读数（Windows 11 风格），配合 `CpuPLFixSvc` + `T14sFanSvc` 使用。

## 它不碰硬件

两个服务每拍把读数写到 `..\status\cpu.json` / `..\status\fan.json`，
HUD 只读文件 —— 所以**不需要管理员权限**，也不会去抢 EC 总线。

## 使用

| 操作 | 效果 |
|---|---|
| 左键拖动 | 移动位置（自动记住） |
| 右键 | 勾选显示项 / 尺寸（小·标准·大·特大）/ 外观（跟随系统·深色·浅色）/ 底框 / 开机自启 / 恢复默认位置 / 退出 |
| 托盘图标双击 | 显示-隐藏（图标是三叶风扇，颜色随温度） |

显示项：CPU 频率、EC 温度、DTS 温度、风扇转速、风扇档位、封装功率、PL1/PL2、内存、电池、保护状态、时间。

## 实现

- 纯 `ctypes`（共用运行时是 embeddable 版，没有 tkinter / Pillow）。
- 半透明圆角底 + 抗锯齿小字：逐像素 alpha 合成后走 `UpdateLayeredWindow`
  （不是 `LWA_COLORKEY`，所以小字不会有毛边）。
- 字体 `Segoe UI Variable Text`，字高按任务栏高度的 0.26 倍算，和系统时钟一致。

## 配置 `hud.json`

```json
{
  "enabled": true,
  "fields": ["freq", "temp", "fan"],
  "scale": 1.0,
  "theme": "auto",
  "pill": true,
  "x": null,
  "y": null,
  "offset_right": 470
}
```

## 开关

- 开：`5-任务栏显示-开.bat`（或双击 `hud.py`）
- 关：`6-任务栏显示-关.bat`
- 开机自启：右键菜单勾选（写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`）

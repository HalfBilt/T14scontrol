# ec-fix · 2026-10-23 那一轮 EC 修复的一次性工具

> 这是一次性迁移脚本，**跑完就不需要再跑**。留在这里是为了留下"当时到底做了什么"的可执行记录。

## 它做什么

| 步骤 | 内容 |
|---|---|
| 1 | 把 `TSCtl.sys` 从 `%APPDATA%\CpuPLFix`（普通用户可写）复制到 `C:\Windows\System32\drivers\`，SHA256 比对 + Authenticode 验签 |
| 2 | 检查 ACL（`drivers\` 继承下来 Users 只有 ReadAndExecute，安全） |
| 3 | 停 `CpuPLFixSvc` / `T14sFanSvc` / `TSCtl` |
| 4 | 把 `TSCtl` 的 `ImagePath` 重指到 System32 那份 |
| 5 | 重启两个服务，实测 `\\.\TSCtl` 能打开（`DRV_OK`） |
| 6 | **验证失败自动回滚** `ImagePath` 并重启服务 |
| 7 | 把 `dptftcs`（Intel DPTF 遥测服务）改成手动启动并停掉 |

## 怎么跑

双击 `apply-ec-fix.bat`（会弹 UAC）。它会：

1. 清两个 `__pycache__`
2. 对 `CpuPLFix.py` / `fan_control.py` 做语法自检 —— **不过就什么都不改**
3. 调 `apply-l2.ps1` 做上面那 7 步
4. 把 `%APPDATA%\T14scontrol\logs\apply-l2.log` 打印出来

**这个 bat 不依赖自己所在位置**：先找 `%APPDATA%\T14scontrol`，找不到再退到 `%~dp0..\..`。
所以放在仓库里也能直接跑。

## 回滚

脚本自己会在验证失败时回滚。如果要**手动**回到迁移前：

```powershell
sc.exe config TSCtl binPath= "\??\C:\Users\Ting\AppData\Roaming\CpuPLFix\TSCtl.sys"
sc.exe config dptftcs start= auto
sc.exe start CpuPLFixSvc
sc.exe start T14sFanSvc
```

⚠️ 前提是 `%APPDATA%\CpuPLFix` 还在 —— 它已于 10-23 被删除，所以手动回滚实际要先把
`TSCtl.sys` 放回某个路径再把 `binPath` 指过去。

## 相关的代码改动

这一轮真正生效的逻辑在 `CpuPLFix.py` 里，不在本目录：

- `SYS_DST` 优先取 `System32\drivers\TSCtl.sys`
- `_driver_binpath()` / `_set_driver_image_path()` / `_fix_driver_path()` —— **每次服务启动自动校正**登记路径
- `ensure_driver_file()` 的查找顺序：系统目录 → 本目录复制 → 才去全盘找 ThrottleStop.exe
- `tz_temp()` —— 温度改走 ACPI 热区（PDH），不再裸读 EC `0x78`
- `_ec_abort_drain()` / `EC_WRITE_WAIT_S` —— 事务中止补 drain、写事务不半途撒手
- `_ec_first_touch()` —— 冷启动首笔事务前清掉上电遗留的 OBF 字节

详细说明见手册仓库 `thinkpad-t14s-gen4-maintenance` 的 `06-EC现状基线与未决问题.md` §4。

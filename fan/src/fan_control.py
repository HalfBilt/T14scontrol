#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FanControl -- ThinkPad T14s Gen 4 智能风扇控制
重写自 TPFanCtrl2 fork 的核心功能, 修复其两个缺陷:
  1) 启动时按配置文件恢复模式 (Smart/手动), 不再每次都回到 EC 自动
  2) 界面语言/模式等任何变更立即持久化到 config.json

★2026-10-20: 驱动换成 TSCtl, PawnIO 已弃用并卸载
  原因: PawnIO 驱动**拒绝 SYSTEM 账户**(pawnio_open -> 0x80070005),
        风扇没法做成 SYSTEM 常驻服务; TSCtl(CPU 侧一直在用)没这个问题。
  现在直接 import CPU 侧的 CpuPLFix.py, 复用它的 EC/MSR 访问层 —— 好处:
    · 带齐了加固: 哨兵 0xC9 / 一致性校验 / 失败刹车 / 自适应等待 /
      "OBF 挂着就不碰"(不抢系统 ACPI EC 驱动正在等的那个字节)
    · 两边拿同一把 'Global\\CpuPLFix-EC' 锁 -> 两进程真正串行化 ->
      不会再出现"抢走 ACPI 数据 -> ACPI 事件 13 -> _TMP 拿不到温度 ->
      固件报 _HOT -> 强制休眠(事件 88)"
  功能:
  - EC 0x2F 写档位 / 0x78 读温度 / 0x84+0x85 读转速
  - DTS 快速通道: MSR 0x1B1 算出的封装温度比 EC 0x78 快 5~10 秒,
    >= dts_max_c(88°C) 立即拉满 0x40, 不等 EC 温度爬上来。不占 EC 总线。
  - 功耗前馈: RAPL(MSR 0x611) 算封装功耗, >= 45W 持续 3 秒就拉满。
  - 负载前馈: GetSystemTimes(纯 Win32 API, 免驱动), 占用 >= 70%
    持续 5 秒就提前拉满; 回落到 55% 以下持续 10 秒才解除。
  - 曲线按**实测转速**排: 50°C->1档 / 62°C->2档 / 72°C->6档 / 80°C->7档 / 84°C->满速
    (3/4/5 档实测都挤在 ~3180 RPM, 没有中间档的意义, 直接跳过)

用法:
  pythonw fan_control.py            常规启动 (托盘图标)
  python  fan_control.py --console  带控制台日志启动
  python  fan_control.py --selftest 只测曲线/前馈/温度换算逻辑, 不碰 EC
"""

import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # PyInstaller 打包后: 配置/日志放 exe 旁边, 资源从解包目录取
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # 源码运行: src/ 的上一级 = T14scontrol\fan, 配置/日志放那里,
    # 这样和 exe 版行为一致(重装/换 src 都不会丢配置)
    APP_DIR = os.path.dirname(SCRIPT_DIR)
# ★2026-10-20: CPU 侧代码目录 —— 风扇侧复用它的 TSCtl 访问层
CPU_DIR    = os.path.join(os.path.dirname(APP_DIR), "cpu")
# ★2026-10-31: 日志统一收进 fan\logs\（原来散在 fan\ 根目录）
LOG_DIR    = os.path.join(APP_DIR, "logs")
LOG_PATH   = os.path.join(LOG_DIR, "fan_control.log")
CONF_PATH  = os.path.join(APP_DIR, "config.json")

# ---- 开机自启 (计划任务) -------------------------------------------------
TASK_NAME = "FanControl-AutoStart"
if getattr(sys, "frozen", False):
    EXE_FOR_TASK = os.path.abspath(sys.executable)
else:
    _exe = os.path.join(APP_DIR, "FanControl.exe")
    _bat = os.path.join(APP_DIR, "FanControl.bat")
    EXE_FOR_TASK = _exe if os.path.isfile(_exe) else (_bat if os.path.isfile(_bat) else None)

# ---- EC 寄存器 (与本机实测一致) ------------------------------------------
CTRL_PORT, DATA_PORT = 0x66, 0x62
FLAG_OBF, FLAG_IBF   = 0x01, 0x02
CMD_READ, CMD_WRITE  = 0x80, 0x81
EC_FAN     = 0x2F   # HFSP: 低3位=档位 0~7, 0x40=满速, 0x80=交还EC自动
EC_RPM_LO  = 0x84
EC_RPM_HI  = 0x85
EC_CPU_TMP = 0x78
# ★0x31(HUWB/VPON/VRST) **不要写** —— 老 IBM/X60 那代才是 fan select。
#   本机实测写 0 和不写, 读到的转速一模一样, 但 VPON/VRST 位会被清掉。

# ---- HFSP(0x2F) 的合法编码 + 健全性校验 -----------------------------------
# ★2026-10-23: 0x2F 只有下面这几种合法编码。**别的值一律是碰撞读出来的脏数据**,
#   不是"固件写进来的奇怪档位"。
#   证据(2026-10-23 复核): 日志里 72 次"HFSP 被固件改成更弱的值", 读到的分别是
#     0x20 0x2C 0x2E 0x30 0x31 0x32 0x33 0x35 0x54 0x90
#   而 0x20=32°C / 0x2C=44 / 0x2E=46 / 0x30=48 / 0x31=49 / 0x32=50 / 0x33=51 /
#   0x35=53 / 0x54=84 —— **全部落在温度区间**, 就是 EC 0x78/0x7A 那一带的读数。
#   决定性反证: 2026-10-22 04:54~08:20 连续 5 次"被改成 0x20"(档位 0 = 停转),
#     而这整段时间 RPM 稳在 2473→2479, 风扇**根本没停**; 23:06 那次同样,
#     22:51 RPM=2469 -> 23:26 RPM=2466。固件没改档位, 是我们读串了。
#   为什么哨兵没挡住: ec_read_strict 是"哨兵-目标-哨兵"**三笔独立事务**, 而碰撞是
#     按事务随机的 —— 只有中间那笔被撞、前后哨兵都干净时, 它完全看不出来。
#     对 0x2F 这种慢变量, "连读两次必须一致"才是有效校验(见 hfsp_read)。
HFSP_VALID = frozenset([0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x40, 0x80])


def hfsp_sane(v):
    """0x2F 的读数是否可能是真的(滤掉碰撞读出的温度字节)。"""
    return v is not None and v in HFSP_VALID

# ---- MSR (官方 IntelMSR 模块的读白名单里都有, 只能读不能写这些) ----------
MSR_TJMAX        = 0x1A2   # IA32_TEMPERATURE_TARGET, bits 23:16 = TjMax
MSR_THERM_STATUS = 0x1B1   # IA32_PACKAGE_THERM_STATUS, bits 22:16 = 距 TjMax 的余量
MSR_PERF_STATUS  = 0x198   # IA32_PERF_STATUS, bits 15:8 = 当前倍频(x100MHz)
MSR_RAPL_UNIT    = 0x606   # RAPL_POWER_UNIT, bits 12:8 = 能量单位 2^-E 焦
MSR_PKG_ENERGY   = 0x611   # PKG_ENERGY_STATUS, 32 位能量计数器(会回绕)

# ★2026-10-20 本机全档位实测标定(手册 scripts/风扇档位-20261020-075530.txt):
#   0x01=2471  0x02=2858  0x03=3181  0x04=3186  0x05=3167
#   0x06=3456(低温)~3880(热态)  0x07=3858(低温)~4380(热态)  0x40=4807(固定, 20~25 秒到位)
#   ★0x03/0x04/0x05 三档挤在 ~3180, 当不了中间档。
#   ★0x08~0x3F 热态下与 0x07 同速(A/B 差 <10 RPM) —— 本机没有 7 档/满速之间的中间档。
#   ★本机原生自动(0x80)天花板 ≈3900, 到不了 7 档 —— 所以不要指望"交还 EC"能压住温度。
# ★0x06 的实测值是 3456(低温时), 之前写 3600 是抄错的 —— 曲线按 RPM 排必须用实测值。
LEVEL_RPM = {0: 0, 1: 2471, 2: 2858, 3: 3181, 4: 3186,
             5: 3167, 6: 3456, 7: 3858, 64: 4807}

DEFAULT_CONFIG = {
    "backend": "tsctl",       # EC/MSR 后端(现在只有 TSCtl; PawnIO 已删)
    "cycle_sec": 5,          # EC 温度轮询间隔(秒): EC 是慢总线, 与 CPU 工具共用, 别太勤
    "tick_sec": 2,           # 控制决策间隔(秒): DTS/负载/功耗走 MSR+API, 不占 EC
    "hysteresis": 4,         # 降档回差(°C): 低于"当前档触发点 - 4"才降档
    "failsafe_temp": 88,     # EC 0x78 达到该温度无条件拉满速 0x40
    # ---- DTS 快速通道 (MSR 0x1B1, 不占 EC 总线) ----
    "dts_max_c": 88.0,       # DTS >= 该值立即拉满 (0=关闭)
    #   ★92 -> 88: DTS 是核心温度, 比 EC 0x78 快 6~13°C。曲线里 EC 到 84 才满速,
    #   等到 DTS 92 早就晚了(手册记的"7 档爬到 95°C"锯齿就是这么来的)。
    # 曲线输入温度 = max(EC 0x78, DTS - dts_lead_c)
    #   DTS 就是核心温度, 升载瞬时比 EC 高 6~13°C; 减 6 后与 EC 同刻度,
    #   既保留原来按 EC 标定的曲线, 又能在急升时提前 5~8 秒升档。
    "dts_lead_c": 6.0,
    "dts_use_min_w": 25.0,  # ★DTS 只在封装功耗 >= 该值时才参与曲线(轻载单核尖峰不算热)
    #   ★2026-10-20: 实测 PKG=19W / EC=67°C 时 DTS 能到 85, 减 lead 后 79 -> 判 7 档,
    #   听感是"没干活风扇却在提速"。功耗低于该值就只用 EC 定档; 0=不做这道门。
    #   硬阈值 dts_max_c(>=88 拉满) 不受这道门影响, 仍随时生效。
    # ---- 功耗前馈 (RAPL: MSR 0x611/0x606, 最直接的"发热量") ----
    "pkg_boost_w": 50.0,     # 封装功耗 >= 该值
    "pkg_boost_s": 4.0,      #   持续该秒数 -> 抬到 pkg_boost_level
    "pkg_boost_level": 7,    #   ★前馈目标档位(默认 7=3858); 满速只留给真温度
    #   ★2026-10-20 优化: 原来 45W/3 秒直接跳满速 0x40(4807) —— 45W 在 1360P 上很常见
    #   (开网页/解压/VS Code 索引), 一跳就是满速再叠加 60 秒驻留, 用户听感是
    #   "什么都没干风扇狂转一分钟"。现在: 前馈只抬到 7 档, 满速交给曲线>=80°C /
    #   DTS>=dts_max / EC>=failsafe 这些**真温度**条件; 阈值也提到 50W/4 秒。
    "pkg_release_w": 38.0,   # 回落到该值以下
    "pkg_release_s": 10.0,   #   持续该秒数才解除
    # ---- 负载前馈 (GetSystemTimes, 免驱动) ----
    "load_boost_pct": 70.0,  # 占用 >= 该值
    "load_boost_s": 5.0,     #   持续该秒数 -> 抬到 load_boost_level
    "load_boost_level": 7,   #   前馈目标档位 (0=关闭前馈); ★64->7: 同上, 别一跳满速
    "load_release_pct": 55.0,   # 占用回落到该值以下
    "load_release_s": 10.0,     #   持续该秒数才解除前馈
    # ---- 降档保护 ----
    "dwell_s": 90,           # 换档后 N 秒内不降档(防边界横跳); 0=不限制
    #   ★25 -> 60 -> 90: 实测日志里档位每 20~90 秒就跳一次(0x02<->0x03<->0x06),
    #   风扇会一直"呜——呜——"地换挡。降档驻留拉长到 90 秒, 升档仍然立即。
    #   ★2026-10-20 补温度条件(dwell_hold): 驻留只在"温度仍在当前档触发点
    #   dwell_temp_margin 以内"时才压住; 掉得够远立刻降档。否则一次 3 秒尖峰
    #   拉起档位后, 温度早回 50°C 了还要白吹 90 秒。
    "dwell_temp_margin": 10.0,   # 驻留判据的温度余量(°C); 0=退回"无条件驻留"
    # ★2026-10-21: 满速档单独给更短的驻留。0x40 比热态 7 档(~4380)高 ~420 RPM
    #   (低温标定时 7 档只有 3858, 差 ~950 —— 曲线用的 72~80°C 段是热态),
    #   满载结束后多吹 90 秒太吵; 而它在高功耗下本来就会被下面的功耗门压住。
    "full_dwell_s": 45,      # 满速档驻留秒数; 0=沿用 dwell_s
    # ★2026-10-21: 满速档不能只看温度退出。28W 满载时 7 档压不住(实测爬到 95°C),
    #   而 EC 慢/DTS 有抖动, 可能短暂掉到 74°C 以下; 只要封装功耗仍 >= 25W,
    #   就保持满速。0=关闭这道门(仅按温度退出)。
    "full_exit_pkg_w": 25.0,
    # 满速档的退出温度: 28W 空载回落时 7 档仍能接住, 但满载时 7 档压不住,
    # 不设这个值就会每 25 秒"满速<->7 档"锯齿一次(见 decide_level 注释);
    # 0=关闭, 退化成普通滞回
    "max_exit_c": 74.0,
    #   ★78 -> 74: 满速的**退出**温度必须明显低于进入温度(84), 因为 0x40 要 20~25 秒
    #   才爬到位、退回 7 档(3858)又要重新爬。实测满速与 7 档降温能力相同(ΔT 都 +33°C),
    #   所以满速的价值是"余量", 不需要一直占着; 74°C 退出既能稳住温度,
    #   又不会退到 7 档后立刻又热(旧值 78 退出后 7 档会一路爬到 95°C)。
    "mode": "smart",         # smart(智能) | manual(手动) | max(最强散热) | auto(EC自动,备用)
    "manual_level": 6,       # 手动模式档位 (0-7, 64=满速)
    # 曲线刻度 = 上面的"曲线输入温度"(EC 与 DTS 校准到同一刻度)
    #   ★2026-10-20 按**实测转速**重排(旧曲线用档位号排, 3/4/5 档挤在 ~3180 转,
    #     物理上没区别, 却占了三段温度区间 -> 温度在 45~65 之间反复换档)。
    #   ≤50°C  -> 1档(2471)  : 最低档用 0x01 不用 0x00, 停转后遇突发要从零起步
    #   50~62°C-> 2档(2858)  : 日常
    #   62~72°C-> 6档(3456)  : 中载; 3/4/5 档(3181/3186/3167) 转速几乎相同, 直接跳过
    #   72~80°C-> 7档(3858)  : 84°C 拉满之前的缓冲档
    #   ≥84°C  -> 满速(4807)  : 0x40 要 20~25 秒才到位, 所以阈值必须靠前
    "curve": [[50, 1], [56, 2], [60, 3], [66, 6], [72, 7], [80, 64]],
    #   ★2026-10-20 定稿(v4)。语义 **temp >= t 才升到该档**, 每点覆盖 [t_i, t_{i+1}):
    #     <56   -> 1 档 2471
    #     56~60 -> 2 档 2858   (落差 387 RPM)
    #     60~66 -> 3 档 3181   (落差 323 RPM) ★过渡档: 本机空闲正好在 57~62 之间晃,
    #     66~72 -> 6 档 3456   (落差 275 RPM)   这一带用细阶梯, 换挡听不出来
    #     72~80 -> 7 档 3858   (落差 402 RPM)
    #     >=80  -> 满速 4807   (落差 949 RPM, 只在真热时出现)
    #   演变: v1 [[50,1],[62,2],[72,6],[80,7],[84,64]] -> 62~72 只有 2 档(退步);
    #         v2 [[50,1],[56,6],[72,7],[80,64]]       -> 56°C 处落差 985 RPM, 空闲抖 5 次/7 分;
    #         v3 [[50,1],[56,2],[62,6],[72,7],[80,64]] -> 62°C 处落差 598 RPM, 仍抖 4 次/5 分;
    #         v4 即本值: 把低中温段拆成 1/2/3/6 四级, 每级 ≤400 RPM。
    #   (3/4/5 档实测 3181/3186/3167 几乎相同, 只用 3 档当过渡, 4/5 跳过)
}


# ==========================================================================
# ==========================================================================
# TSCtl 后端 (2026-10-20) —— 取代 PawnIO
# ==========================================================================
# 为什么换: PawnIO 驱动**拒绝 SYSTEM 账户**(pawnio_open 返回 0x80070005),
#   所以"风扇做成 SYSTEM 服务"在 PawnIO 上走不通。TSCtl(CPU 侧一直在用)没有这个问题。
# 怎么实现: 直接 import CPU 侧的 CpuPLFix.py, 复用它那套已加固的访问层
#   (哨兵 0xC9 / 一致性校验 / 失败刹车 / 自适应等待 / "OBF 挂着就不碰")。
#   顺带好处: 两边拿同一把 'Global\\CpuPLFix-EC' 锁 -> 真正串行化 ->
#   不会再"抢走系统 ACPI EC 驱动正在等的字节" -> ACPI 事件 13 -> _TMP 失败
#   -> 固件报 _HOT -> 强制休眠(事件 88)。
class TSCtlBackend(object):
    """TSCtl 端口 IO + MSR 读。接口与 PawnIO 的用法保持一致。"""

    def __init__(self, cpu_dir=None):
        self._cpf = None
        cpu_dir = cpu_dir or CPU_DIR
        try:
            import importlib.util
            path = os.path.join(cpu_dir, 'CpuPLFix.py')
            spec = importlib.util.spec_from_file_location('cpf_shared', path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            if not m.ensure_driver(verbose=False):
                raise RuntimeError('TSCtl 驱动加载失败(需要管理员)')
            # ★2026-10-20: 风扇控制循环绝不能为了等 EC 锁卡十几秒 ——
            #   实测重载时"循环空档 10~22 秒"反复出现(29 次/共 368 秒)。
            # ★2026-10-21 修正: 以前 _ec_lock 默认 timeout=None(INFINITE), 这里设的
            #   400ms 从没生效过; 现在锁等待和 IBF 等待都按下面的值封顶 ——
            #   拿不到就当这一拍没读到(用缓存值), 下一拍再来。
            m.EC_LOCK_TIMEOUT_MS = 250
            m.EC_WAIT_TIMEOUT_S = 0.2
            self._cpf = m
        except Exception as exc:
            raise RuntimeError('TSCtl 后端初始化失败: %s' % exc)

    def ec_read(self, offset):
        return self._cpf.ec_read_strict(offset)

    def ec_read_fast(self, offset):
        """只做一轮(哨兵+目标+哨兵), 拿不到立刻返回 None —— 给控制循环用。"""
        return self._cpf.ec_read_strict(offset, tries=1)

    def ec_write(self, offset, value):
        return bool(self._cpf.ec_write(offset, value & 0xFF))

    def set_fan(self, level):
        """写 HFSP(0x2F)。★不写 0x31(它不是 FANSW, 写它会清 VPON/VRST, 转速也不变)。"""
        return self.ec_write(EC_FAN, level)

    def hfsp_read(self):
        """读 0x2F 并做健全性校验; 两次不一致 / 非法编码都返回 None。

        ★2026-10-23: 换掉 ec_read_fast(ec_read_strict tries=1)。那套"哨兵-目标-哨兵"
          要 3 笔事务, 却挡不住"只有中间那笔被撞"的情况(碰撞按事务随机发生)。
          0x2F 是慢变量, 用 ec_read_ok(tries=2) 的"连读两次一致"更有效,
          而且只要 2 笔事务 —— 比原来更省总线。
          再叠一道 hfsp_sane: 不是合法编码的值一律当读失败, 绝不用它去重写档位
          (那 72 次"固件抢档"的假警报就是这么来的)。
        """
        try:
            v = self._cpf.ec_read_ok(EC_FAN, tries=2)
        except Exception:
            return None
        return v if hfsp_sane(v) else None

    def tz_temp(self):
        """ACPI 热区温度(°C)。不占 EC 总线; 不可用时返回 None。"""
        try:
            return self._cpf.tz_temp()
        except Exception:
            return None

    def uptime(self):
        """系统清醒运行秒数; 拿不到返回 -1。"""
        try:
            u = self._cpf.unbiased_uptime()
            if u is None:
                u = self._cpf.uptime()
            return float(u)
        except Exception:
            return -1.0

    def boot_quiet(self):
        """开机静默窗口内返回 True —— 此时不要碰 EC 总线。

        ★2026-10-23: 机制保留但**当前关闭**(CpuPLFix.EC_BOOT_QUIET_S = 0)。
          实测过 30 秒窗口: 事件 15 只是从开机第 14 秒推迟到第 31 秒, 数量不变 ——
          无效, 所以关掉, 免得白白让开机头 30 秒跑更弱的原生曲线。
        这里先判 <=0 再返回: 关闭时不做 QueryUnbiasedInterruptTime 那次系统调用,
          免得每个决策节拍都白跑一次。
        """
        try:
            q = float(self._cpf.EC_BOOT_QUIET_S)
            if q <= 0:
                return False
            return 0.0 <= self.uptime() < q
        except Exception:
            return False

    def rpm(self):
        # 转速只用于显示: 用较轻的一致性读(每寄存器 2 笔), 不用 9 笔的 strict ——
        # 满载 + HWiNFO 抢 EC 时, 两次 strict 转速读就能把控制循环拖住十几秒。
        lo = self._cpf.ec_read_ok(EC_RPM_LO, tries=2)
        hi = self._cpf.ec_read_ok(EC_RPM_HI, tries=2)
        if lo is None or hi is None:
            return None
        return (hi << 8) | lo

    def read_msr(self, reg):
        return self._cpf.rdmsr(reg)

    def close(self):
        # ★不要清掉 _cpf: 这个后端对象是 EC 通道和 DTS 通道**共用**的
        #   (MSRThermal.close() 会调它) —— 清了就把 EC 通道也一起弄坏。
        pass


# ==========================================================================
# DTS 快速通道 (MSR 0x1B1 + 0x1A2) —— 独立 PawnIO 实例, 不占 EC 总线
# ==========================================================================
class MSRThermal(object):
    """读 CPU 封装温度 (DTS)、实时倍频、封装功耗.

    温度 = TjMax - (0x1B1 >> 16 & 0x7F)。0x1B1 存的是"距 TjMax 的余量",
    直接当摄氏度读会得到 0°C 之类的荒谬值(早期脚本踩过)。
    ★注意: 满载时这个寄存器的读数会偏保守(有时反而走低), 所以它只当
      "提前量"用; 真正的兜底仍然是 EC 0x78 的 88°C 失效保护。

    功耗 = RAPL 能量计数器 (0x611) 的差分 / 时间差:
      1 LSB = 2^-E 焦, E 来自 0x606 bits 12:8 (本机通常 E=14, 即 61 µJ)。
      32 位计数器会回绕, 所以差分用 & 0xFFFFFFFF。
    """

    def __init__(self, backend=None):
        # ★2026-10-20: 默认走 TSCtl 后端(与 EC 同一个实例, 少一个驱动句柄);
        #   PawnIO 的 IntelMSR 模块保留为可选。
        self.io = backend if backend is not None else TSCtlBackend()
        self.tjmax = self._read_tjmax()
        self.energy_unit = self._read_energy_unit()
        self._e_last = None
        self._e_t = None

    def _read_tjmax(self):
        try:
            v = self.io.read_msr(MSR_TJMAX)
            t = (v >> 16) & 0xFF
            return t if 50 <= t <= 120 else 100
        except Exception:
            return 100

    def _read_energy_unit(self):
        try:
            e = (self.io.read_msr(MSR_RAPL_UNIT) >> 8) & 0x1F
            return 1.0 / float(1 << e)
        except Exception:
            return 1.0 / float(1 << 14)

    def _power(self, now):
        """两次采样之间的平均封装功耗 (W); 第一次采样只建基准返回 None."""
        try:
            e = self.io.read_msr(MSR_PKG_ENERGY) & 0xFFFFFFFF
        except Exception:
            return None
        prev, prev_t = self._e_last, self._e_t
        self._e_last, self._e_t = e, now
        if prev is None or prev_t is None:
            return None
        dt = now - prev_t
        if dt <= 0 or dt > 10:               # 断档太久(如 MSR 读取失败)就不要这次的值
            return None
        d = (e - prev) & 0xFFFFFFFF          # 处理 32 位回绕
        w = d * self.energy_unit / dt
        return w if 0 <= w <= 200 else None

    def read(self, now=None):
        """返回 dict(temp, thermal_status, ratio, power_w); 失败返回 None."""
        if now is None:
            now = time.time()
        try:
            v = self.io.read_msr(MSR_THERM_STATUS)
            dts = (v >> 16) & 0x7F
            temp = self.tjmax - dts
            if temp < 20 or temp > 120:      # 明显是总线/寄存器抽风
                return None
            ratio = None
            try:
                ratio = (self.io.read_msr(MSR_PERF_STATUS) >> 8) & 0xFF
            except Exception:
                pass
            return {"temp": temp, "thermal_status": v & 1, "ratio": ratio,
                    "power_w": self._power(now)}
        except Exception:
            return None

    def close(self):
        self.io.close()


# ==========================================================================
# 系统 CPU 占用 (纯 Win32 API, 免驱动)
# ==========================================================================
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


_PREV_CPU_TIMES = None


def cpu_load_pct():
    """全系统 CPU 占用率 0~100 (GetSystemTimes, 不需要任何驱动).

    第一次调用只建基准返回 None; 之后每次算与上次的差值, 所以调用间隔
    就是统计窗口(控制循环 5 秒一次正合适)。
    kernel 时间包含 idle, 因此"忙" = 总增量 - idle 增量。
    """
    global _PREV_CPU_TIMES
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None

    def _v(f):
        return (f.dwHighDateTime << 32) | f.dwLowDateTime

    i, k, u = _v(idle), _v(kernel), _v(user)
    prev, _PREV_CPU_TIMES = _PREV_CPU_TIMES, (i, k, u)
    if prev is None:
        return None
    d_idle = i - prev[0]
    d_all = (k - prev[1]) + (u - prev[2])
    if d_all <= 0:
        return None
    return min(100.0, max(0.0, 100.0 * (d_all - d_idle) / d_all))


class LoadFeedforward(object):
    """占用 >= pct 持续 hold_s 秒 -> 激活; 回落到 rel_pct 以下持续
    rel_s 秒 -> 解除。进入/退出用两套阈值(70/55), 防止在阈值上抖动。"""

    def __init__(self):
        self.active = False
        self._start = None
        self._cool = None

    def reset(self):
        """清空计时. 休眠/唤醒后必须清 —— 几个小时的睡眠会让
        `now - _start` 变成一个巨大的数, 一醒来就误判成"负载持续很久"."""
        self.active = False
        self._start = None
        self._cool = None

    def update(self, load, now, pct, hold_s, rel_pct, rel_s):
        if load is None:                 # 读不到就保持现状, 别乱切
            return self.active
        if self.active:
            if load < rel_pct:
                if self._cool is None:
                    self._cool = now
                if now - self._cool >= rel_s:
                    self.active = False
                    self._start = None
                    self._cool = None
            else:
                self._cool = None
            return self.active
        if load >= pct:
            if self._start is None:
                self._start = now
            if now - self._start >= hold_s:
                self.active = True
                self._cool = None
        else:
            self._start = None
        return self.active


# ==========================================================================
# 曲线决策 (含滞回), 可独立自测
# ==========================================================================
def ec_temp_ok(v):
    """EC 0x78 的有效范围 0~127°C。

    2026-10-20 实测到 EC 偶发返回 132 —— 这种越界值是总线/固件抽风,
    必须丢弃, 否则会误触发 88°C 失效保护, 把风扇莫名钉到满速。
    """
    return v is not None and 0 <= v <= 127


def blend_temp(ec, dts, lead):
    """曲线输入温度 = max(EC, DTS - lead)。

    DTS 是核心温度, 急升时比 EC 高 6~13°C; 减去 lead 后与 EC 同刻度。
    稳态时两者接近, 取大值不会让风扇一直偏吵; 冲载时 DTS 提前几秒顶上去。
    """
    if dts is None:
        return ec
    if ec is None:
        return max(0, dts - lead)
    return max(ec, dts - lead)


def dwell_hold(temp, curve, level, max_exit, margin):
    """降档驻留期内, 还要不要为了"别抖"而压住不降?

    ★2026-10-20: 原来的驻留是**无条件**的 —— 换档后 60 秒内一律不降档。于是一次 3 秒的
    功耗/负载尖峰把档位拉上去, 哪怕温度已经掉回 50°C, 也要白吹满 60 秒(用户听感就是
    "风扇突然狂转一分钟")。旧版单进程时代其实有温度条件(temp > 触发点-10), 拆分时漏了。

    现在的判据: 温度**仍在当前档触发点 margin 以内**才压住; 掉得够远就立刻降。
      · 普通档: 触发点 = 曲线里这一档的温度(如 6 档 = 72°C)
      · 满速档: 触发点取 max_exit(默认 74°C, 本身就是"低画"的退出点)
    temp 为 None(读不到)时保守压住, 不冒险降。
    """
    if temp is None:
        return True
    trig = None
    if max_exit is not None and curve and level == curve[-1][1]:
        trig = max_exit
    else:
        for t, lv in curve:
            if lv == level:
                trig = t
                break
    if trig is None:
        return True                     # 找不到触发点(如 0x80/0x00)就别乱降
    return temp > trig - margin


def decide_level(temp, curve, current, hysteresis, max_exit=None):
    """curve: [[temp, hfsp], ...] 按温度升序. 返回目标 HFSP 值.
    升档立即; 降档需温度低于当前档触发点 - hysteresis (防抖动).

    max_exit: 曲线最高档(本机=满速 0x40)的**退出**温度, None = 与其他档一样用滞回.
      2026-10-20 实测出来的锯齿: 28W 满载时 7 档(3858) 压不住, DTS 一路爬到 92~95
      -> 拉满 -> 降到 85 -> 驻留期满退回 7 档 -> 再爬上来, 每 25~26 秒一轮
      (11:41:49 满 / 11:42:15 七档 / 11:42:45 满 / 11:43:11 七档 ...)。
      满速档 0x40 光爬速就要 20~25 秒, 退回 7 档等于白爬。
      把满速的退出温度单独画低(当前 74°C = 进 80 退 74), 28W 稳态就一直待在满速
      (同负载实测: 满速 -> 核心稳在 80°C; 7 档 -> 爬到 95°C), 锯齿消失。
    """
    if not curve:
        return current
    # ★2026-10-21: 0x80(EC 自动)/0x00/0x31 等不是曲线里的档位。以前拿它当"当前档位"参与
    #   "降一级"计算 —— 它比满速 0x40 还大, 算出来先写 0x40, 下一拍再退 7 档。开机/唤醒
    #   后固件常留下 0x80, 于是每次接管都先吹 2 秒满速(日志 08:12 / 08:24 两次都是)。
    #   未知值一律按"没读到档位"处理, 直接按温度给目标。
    if current is not None and current not in [lv for _t, lv in curve]:
        current = None
    target = curve[0][1]
    for t, lv in curve:
        if temp >= t:
            target = lv
    if current is None:
        return target
    if target >= current:
        return target
    if max_exit is not None and current == curve[-1][1]:
        if temp >= max_exit:
            return current
        lower = [lv for _t, lv in curve if lv < current]   # 退出满速也只退一级
        return lower[-1] if lower else target
    # 降档: 找到当前档位对应的温度点
    tcur = None
    for t, lv in curve:
        if lv == current:
            tcur = t
    # ★2026-10-20 修一个真缺陷: 满速 0x40 不在曲线的普通档里(曲线最高档的值才是它),
    #   所以 tcur 会是 None -> 旧代码**直接返回 target**(退到 6 档), 把回差整条绕过了,
    #   满速档就会在 80°C 附近来回跳(进 84 退 79)。
    #   正确做法: 满速的触发点就是曲线最高那一档的温度(84), 回差照它算。
    if tcur is None and current == curve[-1][1]:
        tcur = curve[-1][0]
    if tcur is not None and temp >= tcur - hysteresis:
        return current
    # ★2026-10-20 修"忽大忽小": 降档一次只降一级。
    #   直接按曲线降会 0x06 -> 0x02 (3456 -> 2858 RPM, 一次 600 RPM 陡降),
    #   温度稍一回升又立刻升到 3 档, 听感就是转速在两级之间来回摆。
    #   限制每拍最多下一级(节拍 2 秒), 总下降时间只多几秒, 但转速是滑下去的。
    if target < current:
        lower = [lv for _t, lv in curve if lv < current]
        if lower and target < lower[-1]:
            target = lower[-1]
    return target


def wake_gap(prev, now, tick):
    """循环空档是否大到可以判定"中间睡过"。

    内核进入 S3/S4 时本线程也跟着停摆, 醒来后 `now - prev` 会跳一大截。
    本机固件 OWAK() 会把 HFSP 抢成 0x07/0x80, 必须立刻核对重写,
    否则要等 30 秒的转速轮询才纠正(实测 11:47:42 唤醒 -> 11:48:06 才纠正,
    那 24 秒里风扇停在 0x80 自动档, 核心正冲到 91°C)。
    """
    if not prev:
        return False
    return (now - prev) > max(6.0, float(tick) * 3)


# ==========================================================================
# 控制器
# ==========================================================================
class FanController(object):
    def __init__(self, console=False, log_name=None):
        self.console = console
        self.log = self._make_logger(log_name)
        self.cfg = self._load_config()
        self.lock = threading.Lock()
        self.ec = None
        self.stop_evt = threading.Event()
        self.exit_action = "keep"       # 退出永远保持当前档位 (EC 自动已失效, 不交还)
        # 运行状态
        # ★2026-10-20: 托盘已删除 -> mode 只能是 smart。
        #   config.json 里若残留 mode=auto(旧托盘写的), 以前会让服务"完全交还固件",
        #   固件在 60~63°C 只给 0x01(2471 RPM) —— 那就是"风扇几乎不转"的直接原因。
        #   现在服务一律智能模式, 并把 config 里的 mode 改回来。
        self.mode = "smart"
        self.manual_level = int(self.cfg.get("manual_level", 6))
        # ★2026-10-20: config.json 里是旧值(mode=auto / 旧曲线 / dts 92 / dwell 25),
        #   而 _load_config 是"config 覆盖默认值" -> 旧值会把新默认全顶掉。
        #   托盘已删, 没人改配置了, 所以这里把 config 同步成当前默认(只补齐缺的/过时的键)。
        if self._sync_config_to_defaults():
            self.cfg.update({k: v for k, v in DEFAULT_CONFIG.items()
                              if k in ("mode", "backend")})
        if self.mode not in ("smart", "manual", "max", "auto"):
            self.mode = "manual" if self.mode == "silent" else "smart"
            self.cfg["mode"] = self.mode
        self.current_hfsp = None
        self.last_temp = None
        self.last_rpm = None
        self.last_err = ""
        # DTS / 负载前馈 状态
        self.msr = None                 # MSRThermal 实例(可缺席)
        self.last_dts = None            # DTS 温度 °C
        self._dts_prev = None           # 上一拍的 DTS(滤单拍尖峰用)
        self.last_load = None           # 系统占用 %
        self.last_pkg = None            # 封装功耗 W
        self.ff = LoadFeedforward()     # 负载前馈
        self.ff_pkg = LoadFeedforward() # 功耗前馈
        self.ff_active = False
        self.pkg_active = False
        self._dts_latch = False         # DTS 是否处于"已过热"锁存(带 4°C 回差)
        self._last_change = 0.0         # 上次换档的时间戳(用于 dwell)
        self._last_reason = ""          # 上次拉满的原因(给日志/托盘)
        # EC 采样缓存(决策 2 秒一次, EC 仍按 cycle_sec 轮询)
        self._ec_temp = None
        self._ec_src = None             # 温度来源: "TZ"(ACPI 热区) / "EC"(裸读 0x78)
        self._ec_t = 0.0
        self._ec_temp_t = 0.0           # 最近一次**读成功**的时间(HUD 判断温度是否陈旧)
        self._ec_bad = 0
        self._rpm = None
        self._rpm_t = 0.0               # 上次读转速的时间戳
        self._rpm_fast_until = 0.0      # 换档后的一段时间内把转速采样提速(见 _ec_poll)
        self._hfsp_check_t = 0.0        # 上次核对 0x2F 的时间(见 _guard_hfsp)
        self._last_tick = 0.0           # 上一轮决策的时间戳(用于识别睡眠空档)

    # ---- config ----------------------------------------------------------
    def _load_config(self):
        try:
            # ★用 utf-8-sig: PowerShell 的 Set-Content -Encoding UTF8 会写 BOM,
            #   用 utf-8 读会直接抛 "Unexpected UTF-8 BOM" -> 配置被当成损坏而重建(丢设置)。
            with open(CONF_PATH, "r", encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
            return merged
        except FileNotFoundError:
            self._write_config(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)
        except Exception as exc:
            try:
                os.rename(CONF_PATH, CONF_PATH + ".broken")
            except Exception:
                pass
            self.log.warning("配置损坏(%s), 已用默认配置重建", exc)
            self._write_config(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)

    def _sync_config_to_defaults(self):
        """把 config.json 里**会导致功能失效**的两个键纠正过来(返回 True 表示动过)。

        ★2026-10-20 收窄: 原来这里会把 mode/curve/hysteresis/dts_max_c/dwell_s/max_exit_c/
        cycle_sec/tick_sec/backend 九个键一律同步成 DEFAULT_CONFIG —— 于是用户改 config.json
        调曲线或驻留, 重启就被静默改回代码默认值, "改配置"根本存不住。
          现在只兜两个**写错就出事**的键:
            · mode   = smart  (旧托盘写过 auto -> 完全交还固件 -> 60~63°C 只给 0x01, 即"风扇几乎不转")
            · backend = tsctl (PawnIO 已删, 写别的打不开 EC)
          其余一律以 config.json 为准(它才是调参入口)。
        """
        need = {k: v for k, v in DEFAULT_CONFIG.items()
                 if k in ("mode", "backend")
                 and self.cfg.get(k) != v}
        if not need:
            return False
        try:
            self.cfg.update(need)
            self._write_config(self.cfg)
            self.say("config 已同步到当前默认: %s"
                     % ", ".join("%s=%s" % (k, need[k]) for k in sorted(need)))
            return True
        except Exception as exc:
            self.say("config 同步失败: %s" % exc)
            return False

    def _write_config(self, cfg):
        tmp = CONF_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONF_PATH)

    def set_mode(self, mode, manual_level=None):
        with self.lock:
            self.mode = mode
            if manual_level is not None:
                self.manual_level = int(manual_level)
                self.cfg["manual_level"] = self.manual_level
            self.cfg["mode"] = mode
            self._write_config(self.cfg)      # 立即持久化 -- 修复 fork 缺陷
        self.log.info("模式 -> %s%s", mode,
                      (" (档位 %s)" % self.manual_level) if mode == "manual" else "")

    def request_stop(self, exit_action):
        self.exit_action = exit_action
        self.stop_evt.set()

    # ---- logging ---------------------------------------------------------
    def _make_logger(self, log_name=None):
        """log_name=None -> logs/fan_control.log(托盘); 给个名字则写 logs/ 下的同名文件(服务)。"""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except Exception:
            pass
        path = LOG_PATH if not log_name else os.path.join(LOG_DIR, log_name)
        lg = logging.getLogger("fancontrol" + ("." + log_name if log_name else ""))
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            from logging.handlers import RotatingFileHandler
            # 单文件最大 3MB, 超出自动轮转为 .log.1 (共最多占 6MB, 永不无限增长)
            fh = RotatingFileHandler(path, maxBytes=3 * 1024 * 1024,
                                     backupCount=1, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            lg.addHandler(fh)
        return lg

    def say(self, msg):
        self.log.info(msg)
        if self.console:
            try:
                print(msg, flush=True)
            except Exception:
                pass

    # ---- EC / MSR -----------------------------------------------------------
    def _ec_init(self):
        # ★2026-10-20: 后端改成 TSCtl(可配 backend=pawnio 回退)。
        #   TSCtl 与 CPU 侧共用驱动与 'Global\CpuPLFix-EC' 锁 -> 两进程不会插队。
        # ★2026-10-20: 只剩 TSCtl 一个后端(PawnIO 已删: 它拒绝 SYSTEM 账户)
        self.ec = TSCtlBackend()
        self.say("EC 通道就绪 (TSCtl, 与 CPU 侧同一驱动/同一把锁)")
        # ★2026-10-23: 明确报一次温度来源。
        #   日志里的 "EC=xx°C" 现在可能来自 ACPI 热区(正常情况下), 也可能是热区不可用时的
        #   EC 0x78 裸读 —— 两者读数差 1~2°C, 但排查时意义完全不同(裸读意味着又在抢总线)。
        try:
            _tz = self.ec.tz_temp()
            self.say("温度来源: %s" % (
                "ACPI 热区 \\_TZ.THM0 (不占 EC 总线)"
                if _tz is not None else "EC 0x78 裸读 (热区不可用, 会与 ACPI 抢总线)"))
        except Exception:
            pass
        msr_backend = self.ec
        # DTS 通道: 打不开也不影响风扇主功能
        try:
            self.msr = MSRThermal(msr_backend)
            t = self.msr.read()
            self.say("DTS 通道就绪 (IntelMSR, TjMax=%d, 能量单位 %.3fJ/LSB) 当前 DTS=%s"
                     % (self.msr.tjmax, self.msr.energy_unit,
                        ("%d°C" % t["temp"]) if t else "读不到"))
        except Exception as exc:
            self.msr = None
            self.say("DTS 通道不可用 (%s) —— 只靠 EC 温度与负载前馈" % exc)

    def _boot_quiet(self):
        """开机静默窗口内返回 True —— 一律不主动碰 EC 总线。

        ★2026-10-23: 实测开机第 14 秒两个服务同时访问 EC, 而 ACPI 驱动还在做启动
          初始化(热区/电池/DPTF), 固定爆 5 条事件 15。这 30 秒内风扇交给 EC 原生曲线,
          和"服务还没起来"完全一样, 不是失控; 决策仍靠 DTS(MSR, 不占 EC) 正常进行。
        """
        try:
            return self.ec.boot_quiet()
        except Exception:
            return False

    def _ec_poll(self, now):
        """按 cycle_sec 节流取温度; 转速每 6 次 EC 读一次(约 30 秒).

        ★2026-10-23: 温度改走 ACPI 热区(PDH, 零 EC 总线占用), 拿不到才退回裸读 0x78。
          原因: EC 0x78 正是 \\_TZ.THM0._TMP 要读的寄存器, 抢它的终点是
          "ACPI 拿不到温度 -> 固件报 _HOT -> 强制休眠"(2026-10-18 已发生过一次)。
          热区读数与 0x78 偏差 1~2°C, 滞后 1~3 秒; 提前量由 DTS 负责, 够用。

        ★EC 0x78 偶发返回越界值(2026-10-20 实测读到 132°C), 必须丢弃:
          一个假的高温会误触发 88°C 失效保护, 把风扇莫名钉到满速。
          有效范围取 0~127。
        """
        if self._boot_quiet():
            return self._ec_temp, self._rpm
        if self._ec_temp is None or now - self._ec_t >= self._ec_cycle:
            raw = self.ec.tz_temp()
            self._ec_src = "TZ" if raw is not None else "EC"
            if raw is None:
                try:
                    raw = self.ec.ec_read_fast(EC_CPU_TMP)
                except Exception as exc:
                    self._ec_bad += 1
                    if self._ec_bad in (1, 10) or self._ec_bad % 60 == 0:
                        self.say("EC 温度读取失败 %s (连续第 %d 次)" % (exc, self._ec_bad))
                    raw = None
            if ec_temp_ok(raw):
                if self._ec_bad:
                    self.say("EC 温度恢复正常 %d°C (此前连续异常 %d 次, 当前来源 %s)"
                             % (raw, self._ec_bad, self._ec_src))
                self._ec_temp = raw
                self._ec_temp_t = now
                self._ec_bad = 0
            elif raw is not None:
                self._ec_bad += 1
                if self._ec_bad in (1, 10) or self._ec_bad % 60 == 0:
                    self.say("EC 温度读数越界 %d°C, 已丢弃 (连续第 %d 次)"
                             % (raw, self._ec_bad))
            self._ec_t = now

            # 转速 + HFSP 核对
            #   本机固件 OWAK() 里有 If(SCRM) HFSP=0x07, 唤醒后会抢回档位,
            #   不核对的话工具会一直以为还是原来那档, 再也不重写。
            #
            # ★2026-10-20(托盘对不上): 原来固定"每 6 次 EC 读"(约 30 秒)读一次转速,
            #   而 0x40 从写下去到爬稳要 20~25 秒 —— 托盘上那 30 秒显示的是**换档前**的转速,
            #   看着就像"点了满速没反应"。改成: 换档后 35 秒内跟着温度一起读(每 5 秒),
            #   平时仍是 30 秒一次(不增加 EC 总线负担)。
            interval = self._ec_cycle if now < self._rpm_fast_until else 30.0
            if self._rpm is None or now - self._rpm_t >= interval:
                try:
                    v = self.ec.rpm()
                    if v is not None:       # 读失败保留上一次转速, 别让 HUD 闪成 ----
                        self._rpm = v
                        self._rpm_t = now
                except Exception:
                    pass
        return self._ec_temp, self._rpm

    def _guard_hfsp(self, want):
        """守望档位: 固件(联想 BIOS 原生曲线)会持续改写 0x2F, 把我们写的顶掉。

        ★2026-10-20 实测: 压测期间固件连续把 0x2F 改成 0x06/0x03/0x02/0x07,
          连我们写的满速 0x40 都被改成 0x03(3181 RPM) —— 这就是"风扇几乎不转"的真凶。
        所以这里每个决策节拍(2 秒)都核对一次, 被改就立刻重写。
        返回 True = 发生过抢档(调用方要把 want 重新下发)。
        """
        if want is None or self.current_hfsp is None:
            return False
        if self._boot_quiet():
            return False
        # ★2026-10-23: 改用 hfsp_read(两次一致 + 合法编码校验)。
        #   以前这里用 ec_read_fast, 把碰撞读到的温度字节(0x20/0x31/0x54…)当成
        #   "固件把档位改弱了", 于是重写了 72 次 —— 每次还要多 2 笔 EC 事务,
        #   纯属自己给自己制造总线流量。读不准就当这一拍没看到。
        real = self.ec.hfsp_read()
        if real is None:
            return False
        if real == want:
            self.current_hfsp = real
            return False
        # ★只防"变弱": 固件给的档位 >= 我们要的 -> 接受(它在帮我们降温, 别拉锯);
        #   固件给的 < 我们要的 -> 立刻重写(挡住"固件把风扇按到最低档")。
        #   档位比较用"风扇强度"序: 0x40(满速) > 0x07 > ... > 0x01; 0x80/0x00 视为未知,
        #   一律重写(未知值不能当作"够强")。
        def _strength(v):
            if v is None:
                return -1
            if v == 0x40:
                return 100
            if v == 0x80 or v == 0x00:
                return -1              # AUTO / 停转: 未知强度
            return v & 0x07
        if _strength(real) >= _strength(want):
            self.current_hfsp = real   # 固件给得更强 -> 接受, 不写
            return False
        self.say("注意: HFSP 被固件改成 0x%02X(比本工具要的 0x%02X 更弱), 立刻重写"
                 % (real, want))
        self.current_hfsp = want
        return True

    def _wait_tick(self, tick, _stop, _wake):
        """等下一个决策节拍。返回 True = 被"唤醒/电源事件"打断(需要立刻重读)。"""
        if _wake is not None:
            woke = _wake.wait(tick)
            if woke:
                return True
            return False
        self.stop_evt.wait(tick)
        return False

    # ---- control loop --------------------------------------------------------
    def run(self):
        """托盘模式: 自己管停止事件。"""
        try:
            self._ec_init()
        except Exception as exc:
            self.say("FATAL: EC 初始化失败: %s" % exc)
            self.say("请以管理员身份运行 fan 目录下的『4-重启风扇服务.bat』(未安装则先跑『1-安装风扇服务.bat』)。详见 logs\\fan_service.log")
            return
        self._control_loop(None, None, None, None)

    def start_service_loop(self, stop_evt, wake_evt, resume, pbt_name):
        """服务模式: 由 SCM 拉起(SYSTEM 会话, 不依赖用户登录)。

        与托盘模式共用同一套控制循环, 区别只有:
          - 停机由 SCM 的 SERVICE_CONTROL_STOP 驱动;
          - 睡眠/休眠唤醒/拔插电 由 SCM 的 SERVICE_CONTROL_POWEREVENT 通知,
            醒来 1 秒内立刻重写风扇档位(固件 OWAK() 会把 HFSP 抢回 0x07/0x80)。
        """
        try:
            self._ec_init()
        except Exception as exc:
            self.say("FATAL: EC 初始化失败(服务模式): %s" % exc)
            return
        self.say("服务模式: 以 SYSTEM 身份接管风扇(不依赖用户登录)")
        self._control_loop(stop_evt, wake_evt, resume, pbt_name)

    def _pub_status(self):
        """给任务栏 HUD 用的状态快照（原子写，失败不影响控制）。

        ★2026-10-21: 改由独立心跳线程调用。以前它在控制循环里, EC 一卡(HWiNFO
          抢总线 / 满载)循环停摆十几秒, fan.json 就过期 -> HUD 的转速和功率变 --。
          现在控制循环停摆时, 心跳仍用缓存值刷新 ts; 数值没变则最多 10 秒写一次
          (HUD 过期门限 15 秒), 不增加 SSD 写盘。
        """
        try:
            import json as _json
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'status')
            with self.lock:
                obj = {
                    'hfsp': self.current_hfsp,
                    'rpm': self.last_rpm,
                    'ec': self.last_temp,
                    'ec_ts': round(self._ec_temp_t, 2) if self._ec_temp_t else None,
                    'dts': self.last_dts,
                    'pkg': round(self.last_pkg, 1) if self.last_pkg is not None else None,
                    'load': round(self.last_load, 1) if self.last_load is not None else None,
                    'mode': self.mode,
                }
            now = time.time()
            last = getattr(self, '_pub_last', None)
            if last and obj == last[0] and (now - last[1]) < 10.0:
                return
            self._pub_last = (dict(obj), now)
            obj['ts'] = round(now, 2)
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, 'fan.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as fh:
                _json.dump(obj, fh, ensure_ascii=False)
            os.replace(tmp, os.path.join(d, 'fan.json'))
        except Exception:
            pass

    def _pub_loop(self, stop_evt):
        while not stop_evt.wait(2.0):
            self._pub_status()

    def _control_loop(self, stop_evt, wake_evt, resume, pbt_name):
        """主控制循环。stop_evt/wake_evt 为 None 表示托盘模式(用 self.stop_evt)。"""
        cycle = max(1, int(self.cfg.get("cycle_sec", 5)))
        tick = max(1.0, float(self.cfg.get("tick_sec", 2) or 2))
        self._ec_cycle = cycle
        hyst = int(self.cfg.get("hysteresis", 3))
        fs_temp = int(self.cfg.get("failsafe_temp", 88))
        dts_max = float(self.cfg.get("dts_max_c", 92.0) or 0)
        dts_lead = float(self.cfg.get("dts_lead_c", 6.0) or 0)
        dts_use_w = float(self.cfg.get("dts_use_min_w", 25.0) or 0)
        pkg_on = float(self.cfg.get("pkg_boost_w", 45.0) or 0)
        pkg_on_s = float(self.cfg.get("pkg_boost_s", 3.0))
        pkg_off = float(self.cfg.get("pkg_release_w", 35.0))
        pkg_off_s = float(self.cfg.get("pkg_release_s", 10.0))
        boost_pct = float(self.cfg.get("load_boost_pct", 70.0))
        boost_s = float(self.cfg.get("load_boost_s", 5.0))
        boost_lv = int(self.cfg.get("load_boost_level", 7) or 0)
        pkg_lv = int(self.cfg.get("pkg_boost_level", 7) or 0)
        rel_pct = float(self.cfg.get("load_release_pct", 55.0))
        rel_s = float(self.cfg.get("load_release_s", 10.0))
        dwell = float(self.cfg.get("dwell_s", 90) or 0)
        full_dwell = float(self.cfg.get("full_dwell_s", 0) or 0)
        full_pkg_w = float(self.cfg.get("full_exit_pkg_w", 0) or 0)
        dwell_margin = float(self.cfg.get("dwell_temp_margin", 10.0) or 0.0)
        max_exit = float(self.cfg.get("max_exit_c", 74.0) or 0)
        curve = [[int(t), int(v)] for t, v in self.cfg.get("curve", DEFAULT_CONFIG["curve"])]
        curve.sort(key=lambda x: x[0])
        self.say("启动模式: %s   曲线: %s" % (self.mode, curve))
        self.say("节拍: 决策 %gs / EC 轮询 %gs   曲线输入 = max(EC, DTS-%g)"
                 % (tick, cycle, dts_lead))
        self.say("快速通道: DTS %s | 功耗 %s | 负载 %s" % (
            (">=%g°C 拉满" % dts_max) if (dts_max > 0 and self.msr) else "关闭",
            (">=%gW 持续 %gs -> %d 档" % (pkg_on, pkg_on_s, pkg_lv))
            if (pkg_on and self.msr) else "关闭",
            ("占用>=%g%% 持续 %gs -> %d 档" % (boost_pct, boost_s, boost_lv)) if boost_lv else "关闭"))
        self.say("降档: 驻留 %gs/满速 %gs(仅当温度仍在触发点 %g°C 内) | "
                 "满速低于 %g°C 且功耗 < %gW 才退"
                 % (dwell, full_dwell if full_dwell > 0 else dwell,
                    dwell_margin, max_exit, full_pkg_w))
        # ★2026-10-20: 启动/唤醒后**立刻**写一次档位。
        #   实测固件每 ~30 秒按自己的曲线重写 0x2F, 温度 60~63°C 时它会选 0x01(2471 RPM)
        #   —— 服务没在的时候就是这个状态(用户报的"风扇几乎不转")。
        #   所以一接管就先按曲线把档位摆正, 不等下一轮决策。
        try:
            _t0 = self._ec_temp
            if _t0 is not None:
                _lv = decide_level(blend_temp(_t0, self.last_dts, dts_lead), curve,
                                 self._read_hfsp() or 1, hyst, max_exit or None)
                if _lv is not None:
                    self._write_level(_lv, time.time())
                    self.say("接管即摆正档位: -> 0x%02X (%s RPM)"
                             % (_lv, LEVEL_RPM.get(_lv, "?")))
        except Exception as exc:
            self.say("接管时摆正档位失败: %s" % exc)

        err_count = 0
        _stop = stop_evt if stop_evt is not None else self.stop_evt
        _wake = wake_evt
        _pub_stop = threading.Event()
        threading.Thread(target=self._pub_loop, args=(_pub_stop,), daemon=True,
                         name='fan-status').start()
        while not _stop.is_set():
            now = time.time()
            # ★休眠/唤醒检测: 内核挂起时本线程也停摆, 醒来 now 会跳一大截。
            #   立刻重读 EC 温度 + 核对 HFSP, 不等 30 秒轮询(见 wake_gap 注释)。
            if wake_gap(self._last_tick, now, tick):
                self.say("检测到休眠/唤醒 (循环空档 %.0f 秒), 立即重读温度并核对档位"
                         % (now - self._last_tick))
                self._ec_t = 0.0
                self._rpm_t = 0.0
                self._rpm = None
                self.ff.reset()
                self.ff_pkg.reset()
            self._last_tick = now
            try:
                temp, rpm = self._ec_poll(now)
                err_count = 0
                self.last_err = ""
            except Exception as exc:
                temp, rpm = self._ec_temp, self._rpm
                err_count += 1
                self.last_err = str(exc)
                if err_count % 5 == 1:
                    self.say("EC 读取异常 x%d: %s" % (err_count, exc))

            # ---- 快速通道采样: DTS/功耗(MSR) + 系统占用(API), 都不占 EC 总线 ----
            dts = dts_status = pkg = None
            if self.msr is not None:
                r = self.msr.read(now)
                if r:
                    dts, dts_status, pkg = r["temp"], r["thermal_status"], r["power_w"]
            load = cpu_load_pct()
            ff = self.ff.update(load, now, boost_pct, boost_s, rel_pct, rel_s)
            ff_pkg = (self.ff_pkg.update(pkg, now, pkg_on, pkg_on_s, pkg_off, pkg_off_s)
                      if pkg_on else False)
            # DTS 自带 4°C 回差, 免得在阈值上 64<->7 来回切
            # ★2026-10-20 优化: 硬锁存原来"一拍就锁" —— 轻载单核睿频到 88~89°C 就直接拉满速,
            #   2 秒后掉下来又退回(实测 16:51:01 拉满 -> 16:51:03 退 6 档), 听感是"嗡一下"。
            #   现在要求**连续两拍(4 秒)**都 >= dts_max 才锁, 且封装功耗不低于 dts_use_min_w
            #   (轻载的单核尖峰不算整体发热)。解除仍然只要 <= dts_max-4 一拍即可。
            if dts is not None and dts_max > 0:
                if dts >= dts_max:
                    self._dts_hi_n = getattr(self, '_dts_hi_n', 0) + 1
                    if self._dts_hi_n >= 2 and (dts_use_w <= 0 or pkg is None
                                               or pkg >= dts_use_w):
                        self._dts_latch = True
                else:
                    self._dts_hi_n = 0
                    if dts <= dts_max - 4:
                        self._dts_latch = False
            # 固件热状态位(0x1B1 bit0)只在温度确实高时才认 —— 本机固件有"锁存位"
            # 的先例(EC 0x89), 不设温度门的话一个卡住的位会把风扇永久钉在满速。
            bit_hot = bool(dts_status) and dts is not None and dts_max > 0 and dts >= dts_max - 8

            # ★曲线输入: DTS 是核心温度, 急升时比 EC 高 6~13°C。减 dts_lead 拉回
            #   与 EC 同刻度后取两者较大值 —— 稳态时 EC 说了算(不会一直偏吵),
            #   冲载时 DTS 提前 5~8 秒把档位顶上去。
            # ★2026-10-20 优化: DTS 是"最热核", 凉机时单核睿频也能冲到 86°C -> 86-6=80
            #   -> 曲线直接判满速, 这就是"什么都没干风扇也拉满"的一个来源。取连续两拍
            #   的**较小值**(2 秒一拍 = 4 秒窗口): 单拍尖峰被滤掉, 真升温(连续高)照样通过,
            #   代价只是最多延后 2 秒 —— 与 CPU 侧 DTS 快通道"连高两拍"同一套哲学。
            dts_f = dts
            if dts is not None and self._dts_prev is not None:
                dts_f = min(dts, self._dts_prev)
            self._dts_prev = dts
            # ★2026-10-20 优化(第二道): 轻载时 DTS 的**单核睿频尖峰**不代表整体发热 ——
            #   实测 PKG 只有 19W、EC 67°C 时 DTS 能到 85, 减 6 就是 79 -> 曲线判 7 档,
            #   用户听感是"没干活风扇却在提速"。所以只在封装功耗真的有量时才让 DTS 参与;
            #   功耗读不到(None)时保守照用(宁可多吹)。硬阈值 dts_max_c 那条不受影响。
            if dts_use_w > 0 and pkg is not None and pkg < dts_use_w:
                dts_f = None
            t_ctrl = blend_temp(temp, dts_f, dts_lead)

            with self.lock:
                self.last_temp, self.last_rpm = temp, rpm
                self.last_dts, self.last_load = dts, load
                self.last_pkg = pkg
                self.ff_active, self.pkg_active = ff, ff_pkg
                mode = self.mode
            self._pub_status()

            try:
                if mode == "smart":
                    if t_ctrl is None:
                        self._wait_tick(tick, _stop, _wake)
                        continue
                    cur = self.current_hfsp
                    if cur is None:
                        cur = self._read_hfsp()
                    # ★守望: 固件每 ~30 秒抢一次 0x2F; 我们每个 EC 周期(5 秒)查一次就够,
                    #   不必每个 2 秒节拍都查(那是白白增加 EC 事务)。
                    if (cur is not None and now - self._hfsp_check_t >= self._ec_cycle):
                        self._hfsp_check_t = now
                        if self._guard_hfsp(cur):
                            try:
                                self.ec.set_fan(cur)
                            except Exception:
                                pass
                        # 固件给得更强时 _guard_hfsp 已接受并改了 current_hfsp;
                        # 这里必须用最新值决策, 否则下一行会按旧档位把更强的档位写回去。
                        cur = self.current_hfsp
                    target = decide_level(t_ctrl, curve, cur, hyst, max_exit or None)
                    reason = ""
                    # ① 保护类：无条件拉满（温度墙 / DTS / 固件热状态位）
                    if temp is not None and temp >= fs_temp:
                        target, reason = 64, "EC %d°C>=%d°C" % (temp, fs_temp)
                    elif self._dts_latch:
                        target, reason = 64, "DTS %s°C" % (dts if dts is not None else "--")
                    elif bit_hot:
                        target, reason = 64, "固件热状态位"
                    else:
                        # ② 前馈：只抬不压（功耗优先于负载）
                        if ff_pkg and pkg_lv and pkg_lv > target:
                            target, reason = pkg_lv, "功耗 %.0fW" % (pkg or 0)
                        elif ff and boost_lv and boost_lv > target:
                            target, reason = boost_lv, "负载 %.0f%%" % (load or 0)
                    # ③ 驻留：只压"降档"，且温度仍在当前档触发点附近才压
                    #    ★2026-10-21 修: 原来驻留是 elif 链的一环, 只要前馈分支命中就不判断,
                    #      导致"负载抬档后想压回去"可以绕过 60 秒驻留(实测 2 秒就来回一次)。
                    #    ★2026-10-23 修: 必须显式判 current_hfsp is not None ——
                    #      读档位失败/开机静默窗口期间它是 None, 而 None 参与 `<` 比较会抛
                    #      "'<' not supported between instances of 'int' and 'NoneType'",
                    #      实测导致决策循环每 2 秒报一次 "EC 写入异常"。
                    if (self.current_hfsp is not None
                            and target < self.current_hfsp
                            and self.current_hfsp in LEVEL_RPM
                            and dwell > 0 and (now - self._last_change) < dwell
                            and dwell_hold(t_ctrl, curve, self.current_hfsp,
                                           max_exit or None, dwell_margin)):
                        target = self.current_hfsp
                    if target != self.current_hfsp:
                        self._write_level(target, now)
                        self._last_reason = reason
                        self.say("%s  EC=%s DTS=%s PKG=%s  RPM=%s  -> 0x%02X (%s RPM)%s"
                                 % (time.strftime("%H:%M:%S"),
                                    ("%d°C" % temp) if temp is not None else "--",
                                    ("%d°C" % dts) if dts is not None else "--",
                                    ("%.0fW" % pkg) if pkg is not None else "--",
                                    rpm if rpm is not None else "--",
                                    target, LEVEL_RPM.get(target, "?"),
                                    ("   [" + reason + "]") if reason else ""))
                    # 无变化不写日志 (保护 SSD; 状态随时看托盘悬停)
                elif mode == "manual":
                    manual = self.manual_level
                    hot = ((temp is not None and temp >= fs_temp)
                           or self._dts_latch or bit_hot)
                    target = 64 if hot else manual
                    if hot and manual != 64:
                        self.say("!! 失效保护: EC %s°C / DTS %s°C, 手动档临时拉满速"
                                 % (temp if temp is not None else "--",
                                    dts if dts is not None else "--"))
                    if target != self.current_hfsp:
                        self._write_level(target, now)
                        self.say("%s  手动模式: 档位 %s (0x%02X, %s RPM)"
                                 % (time.strftime("%H:%M:%S"),
                                    "满速" if manual == 64 else manual,
                                    target, LEVEL_RPM.get(target, "?")))
                elif mode == "max":
                    if self.current_hfsp != 64:
                        self._write_level(64, now)
                        self.say("%s  最强散热: 满速 0x40 (%s RPM)"
                                 % (time.strftime("%H:%M:%S"),
                                    LEVEL_RPM.get(64, "?")))
                elif mode == "auto":
                    # EC 自动 (0x80 已在切换时写入): 不干预风扇, 只读数显示
                    pass
            except Exception as exc:
                self.last_err = str(exc)
                self.say("EC 写入异常: %s" % exc)
            # ★服务模式: SCM 送来电源事件时立刻醒来重读(固件 OWAK() 会抢 HFSP)
            woke = self._wait_tick(tick, _stop, _wake)
            if woke and wake_evt is not None and resume is not None:
                wake_evt.clear()
                self._ec_t = 0.0
                self._rpm_t = 0.0
                self._rpm = None
                self.ff.reset()
                self.ff_pkg.reset()
                self.say("收到电源事件 0x%04X (%s), 立即重读温度并核对档位"
                         % (resume[0], pbt_name(resume[0]) if pbt_name else "未知"))

        # 收尾
        try:
            self.say("退出, 保持当前档位 0x%02X" % (self.current_hfsp or 0))
        except Exception as exc:
            self.say("收尾写入失败: %s" % exc)
        finally:
            _pub_stop.set()
            try:
                self.ec.close()
            except Exception:
                pass
            if self.msr is not None:
                self.msr.close()

    def _write_level(self, level, now):
        """写档位 + 记录时间 + 打开"转速快采样"窗口。

        0x40 从写下去到爬稳要 20~25 秒, 窗口内转速采样提速(每 5 秒)。
        ★2026-10-20: 固件会抢 0x2F, 所以写完**立刻回读一次**确认落到目标档;
        没落到就再写一次(最多 2 次) —— 只写不管的话, 实际转速由固件曲线说了算。
        """
        if self._boot_quiet():
            return                      # 开机静默窗口: 不碰 EC(见 _boot_quiet)
        wrote = False
        for attempt in (1, 2):
            try:
                ok = self.ec.set_fan(level)
            except Exception as exc:
                self.say("写档位失败: %s" % exc)
                break
            if not ok:
                self.say("写档位 0x%02X 没被 EC 确认, 下一拍再试" % level)
                break
            wrote = True
            # ★2026-10-23: 回读用 hfsp_read(两次一致 + 合法编码), 避免拿脏值判"没落到目标"
            real = self.ec.hfsp_read()
            if real is None or real == level:
                break
            self.say("档位没落到目标: 写 0x%02X 回读 0x%02X, 重试" % (level, real))
        if wrote:
            self.current_hfsp = level
            self._last_change = now
            self._rpm_fast_until = now + 35.0

    def _read_hfsp(self):
        if self._boot_quiet():
            return None                 # 开机静默窗口: 不碰 EC(见 _boot_quiet)
        # ★2026-10-23: hfsp_read(两次一致 + 合法编码), 脏值不再污染 current_hfsp
        v = self.ec.hfsp_read()
        if v is not None:
            self.current_hfsp = v
        return v


# ==========================================================================
def selftest():
    """离线自测: 曲线/滞回、负载前馈状态机、DTS 换算。不碰 EC / MSR。"""
    fails = []

    def check(name, got, want):
        ok = (got == want)
        if not ok:
            fails.append(name)
        print("  [%s] %-34s got=%-5s want=%s" % ("OK" if ok else "!!", name, got, want))

    print("== 1. 曲线决策 (EC 0x78 刻度, 滞回 4; 新曲线按实测 RPM 排) ==")
    curve = DEFAULT_CONFIG["curve"]
    cur = 1
    for temp in [30, 49, 50, 60, 62, 70, 72, 78, 80, 83, 84, 90,
                 83, 79, 76, 74, 72, 66, 62, 55, 50, 40]:
        cur = decide_level(temp, curve, cur, 4)
        print("  %2dC -> 0x%02X (%s RPM)" % (temp, cur, LEVEL_RPM.get(cur, "?")))
    check("最低档是 0x01 不是停转", decide_level(30, curve, 1, 4), 1)
    check("49C -> 1 档", decide_level(49, curve, 1, 4), 1)
    check("50C -> 1 档", decide_level(50, curve, 1, 4), 1)
    check("62C -> 3 档(过渡档)", decide_level(62, curve, 1, 4), 3)
    check("72C -> 7 档", decide_level(72, curve, 2, 4), 7)
    check("80C -> 满速", decide_level(80, curve, 6, 4), 64)
    check("84C -> 满速", decide_level(84, curve, 7, 4), 64)
    check("滞回: 81C 仍满速", decide_level(81, curve, 64, 4), 64)
    # 满速档的回差基准是它自己的触发点 84°C: 80C >= 84-4 -> 仍保持满速
    check("滞回: 80C 仍满速(回差基准=满速触发点 84)", decide_level(80, curve, 64, 4), 64)
    check("满速退出: 75C 仍满速(退 74)", decide_level(75, curve, 64, 4, 74.0), 64)
    check("满速退出: 74C 踩线仍满速", decide_level(74, curve, 64, 4, 74.0), 64)
    check("满速退出: 73C 才退(退到 7 档)", decide_level(73, curve, 64, 4, 74.0), 7)
    # 满速档的触发点是 84 -> 回差 4 -> 80C 才退到曲线档(6 档); 79C 就已经退了
    check("满速退出: 关掉 max_exit 就按普通滞回(79C 仍满速)", decide_level(79, curve, 64, 4, None), 64)
    check("非满速档不受 max_exit 影响(73C 仍是 7 档)", decide_level(73, curve, 7, 4, 74.0), 7)
    # 降档限级: 一次只下一级, 连续调用才走到底
    check("降档限级: 6 档遇 40C 只降到 3 档", decide_level(40, curve, 6, 4), 3)
    check("降档限级: 满速遇 40C 只降到 7 档", decide_level(40, curve, 64, 4, 74.0), 7)
    check("降档限级: 3 档遇 40C 降到 2 档", decide_level(40, curve, 3, 4), 2)
    # 当前档位未知(读失败/刚启动)时直接给曲线目标, 不能拿 None 去比较
    check("当前档未知: 60C -> 3 档", decide_level(60, curve, None, 4), 3)
    check("当前档未知: 72C -> 7 档", decide_level(72, curve, None, 4), 7)
    # 0x80(EC 自动)不是档位: 不能被当成"比满速还高"而先写 0x40
    check("0x80 当前档: 53C -> 1 档", decide_level(53, curve, 0x80, 4, 74.0), 1)
    check("0x80 当前档: 62C -> 3 档", decide_level(62, curve, 0x80, 4, 74.0), 3)

    print("== 2. 负载前馈状态机 (示例参数 70% / 10s 进, 55% / 10s 出) ==")
    ff = LoadFeedforward()
    check("初始未激活", ff.active, False)
    ff.update(90, 0, 70, 10, 55, 10)
    check("90% 只持续 5 秒", ff.update(90, 5, 70, 10, 55, 10), False)
    check("90% 满 10 秒 -> 激活", ff.update(90, 10, 70, 10, 55, 10), True)
    check("掉到 60% 不解除(>55)", ff.update(60, 11, 70, 10, 55, 10), True)
    check("掉到 40% 第 1 秒仍激活", ff.update(40, 12, 70, 10, 55, 10), True)
    check("40% 只过了 9 秒", ff.update(40, 21, 70, 10, 55, 10), True)
    check("40% 满 10 秒 -> 解除", ff.update(40, 22, 70, 10, 55, 10), False)
    check("读数 None 保持现状", ff.update(None, 40, 70, 10, 55, 10), False)
    ff2 = LoadFeedforward()
    ff2.update(65, 0, 70, 10, 55, 10)
    check("65% 不算高负载", ff2.update(65, 20, 70, 10, 55, 10), False)
    ff3 = LoadFeedforward()
    ff3.update(70, 0, 70, 10, 55, 10)
    check("边界 70% 满 10 秒", ff3.update(70, 10, 70, 10, 55, 10), True)
    ff3.reset()
    check("reset 后回到未激活", ff3.active, False)

    print("== 3. DTS 换算 (TjMax - 余量) ==")
    def dts_temp(tjmax, raw):
        return tjmax - ((raw >> 16) & 0x7F)
    check("TjMax=100 余量=8  -> 92°C", dts_temp(100, 8 << 16), 92)
    check("TjMax=100 余量=20 -> 80°C", dts_temp(100, 20 << 16), 80)
    check("余量 0 -> 100°C (TjMax)", dts_temp(100, 0), 100)

    print("== 4. 曲线输入温度 blend (lead=6) ==")
    check("稳态: EC 52 / DTS 58 -> 52", blend_temp(52, 58, 6), 52)
    check("冲载: EC 73 / DTS 86 -> 80", blend_temp(73, 86, 6), 80)
    check("高温: EC 86 / DTS 91 -> 86", blend_temp(86, 91, 6), 86)
    check("无 DTS: 退回 EC", blend_temp(70, None, 6), 70)
    check("无 EC: 用 DTS-lead", blend_temp(None, 80, 6), 74)
    check("实际效果 73/86 拉满(blend=80)",
          decide_level(blend_temp(73, 86, 6), curve, 6, 4), 64)
    check("实际效果 70/76 进 6 档",
          decide_level(blend_temp(70, 76, 6), curve, 2, 4), 6)

    print("== 4b. 降档驻留的温度条件 (dwell_hold, margin=10) ==")
    # 6 档的触发点 = 66°C -> 温度 > 56 才压住
    check("6 档/60°C 仍在余量内 -> 压住", dwell_hold(60, curve, 6, 74.0, 10), True)
    check("6 档/62°C 仍在余量内 -> 压住", dwell_hold(62, curve, 6, 74.0, 10), True)
    check("6 档/57°C 临界(>56)", dwell_hold(57, curve, 6, 74.0, 10), True)
    check("6 档/55°C 越界", dwell_hold(55, curve, 6, 74.0, 10), False)
    # 满速档用 max_exit(74) 当基准 -> 温度 > 64 才压住
    check("满速/70°C 压住", dwell_hold(70, curve, 64, 74.0, 10), True)
    check("满速/60°C 降", dwell_hold(60, curve, 64, 74.0, 10), False)
    check("温度读不到 -> 保守压住", dwell_hold(None, curve, 6, 74.0, 10), True)
    check("未知档位 -> 不乱降", dwell_hold(80, curve, 0x80, 74.0, 10), True)

    print("== 5. EC 温度哨兵 ==")
    check("正常 25°C", ec_temp_ok(25), True)
    check("正常 127°C", ec_temp_ok(127), True)
    check("越界 132°C 丢弃", ec_temp_ok(132), False)
    check("255 丢弃", ec_temp_ok(255), False)
    check("None 丢弃", ec_temp_ok(None), False)

    print("== 6. 休眠/唤醒空档识别 (tick=2 -> 阈值 6s) ==")
    check("首次(prev=0) 不算唤醒", wake_gap(0.0, 1000.0, 2), False)
    check("正常 2s 节拍", wake_gap(1000.0, 1002.0, 2), False)
    check("偶发卡 6s 不误判", wake_gap(1000.0, 1006.0, 2), False)
    check("睡了 60s -> 判定唤醒", wake_gap(1000.0, 1060.0, 2), True)
    check("tick 放大阈值跟着放大", wake_gap(1000.0, 1010.0, 5), False)

    print("")
    if fails:
        print("失败 %d 项: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("全部通过。")
    return 0


def main():
    """入口。★2026-10-20: 托盘已删除(用户要求开机默认就是智能模式)。
    现在只有三种跑法:
      --service   作为 Windows 服务(SYSTEM 常驻; 由 fan_service_ctl.py 注册)
      --console   前台跑, 日志打到控制台(调试用)
      --selftest  离线自测曲线/前馈/温度换算, 不碰 EC
    """
    if "--selftest" in sys.argv:
        return selftest()
    if "--service" in sys.argv:
        # 服务模式: 真正的循环在 FanControlSvc.py 里(SCM 驱动停机/电源事件)
        print("请用 FanControlSvc.py --service 启动服务, 不要直接跑本文件。")
        return 2
    ctrl = FanController(console=("--console" in sys.argv))
    t = threading.Thread(target=ctrl.run, daemon=True)
    t.start()
    try:
        while t.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        ctrl.request_stop("keep")
    return 0
if __name__ == "__main__":
    sys.exit(main())

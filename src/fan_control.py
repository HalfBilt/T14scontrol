#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FanControl -- ThinkPad T14s Gen 4 智能风扇控制 (PawnIO 双模块)
重写自 TPFanCtrl2 fork 的核心功能, 修复其两个缺陷:
  1) 启动时按配置文件恢复模式 (Smart/手动), 不再每次都回到 EC 自动
  2) 界面语言/模式等任何变更立即持久化到 config.json

★2026-10-20 拆分后补回全部能力 (不依赖 TSCtl / ThrottleStop 驱动):
  - 双 PawnIO 模块: LpcACPIEC(读写 EC 0x2F 等) + IntelMSR(只读 MSR)
    ★一个句柄只能加载一个模块 —— 驱动把 blob 绑在文件对象上,
      FsContext 已有值就返回 STATUS_ALREADY_INITIALIZED。
      所以两个模块各开一个 PawnIO 实例(两个句柄)。
  - DTS 快速通道: MSR 0x1B1 / 0x1A2 算出的封装温度比 EC 0x78 快 5~10 秒,
    >= dts_max_c(92°C) 立即拉满 0x40, 不等 EC 温度爬上来。
    MSR 不占 EC 总线, 每轮都读也不会和 CPU 功耗墙工具抢 0x62/0x66。
  - 负载前馈: GetSystemTimes(纯 Win32 API, 免驱动), 系统占用 >= 70%
    持续 10 秒就提前拉满; 回落到 55% 以下持续 10 秒才解除。
  - 新曲线: 最低档用 0x01(不停转), 84°C 前插一档 7 档。

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
    BLOB_PATH = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "LpcACPIEC.bin")
    BLOB_MSR  = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "IntelMSR.bin")
else:
    # 源码运行: src/ 的上一级 = T14scontrol\fan, 配置/日志放那里,
    # 这样和 exe 版行为一致(重装/换 src 都不会丢配置)
    APP_DIR = os.path.dirname(SCRIPT_DIR)
    BLOB_PATH = os.path.join(SCRIPT_DIR, "LpcACPIEC.bin")
    BLOB_MSR  = os.path.join(SCRIPT_DIR, "IntelMSR.bin")
LOG_PATH   = os.path.join(APP_DIR, "fan_control.log")
CONF_PATH  = os.path.join(APP_DIR, "config.json")
DLL_PATH   = r"C:\Program Files\PawnIO\PawnIOLib.dll"

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

# ---- MSR (官方 IntelMSR 模块的读白名单里都有, 只能读不能写这些) ----------
MSR_TJMAX        = 0x1A2   # IA32_TEMPERATURE_TARGET, bits 23:16 = TjMax
MSR_THERM_STATUS = 0x1B1   # IA32_PACKAGE_THERM_STATUS, bits 22:16 = 距 TjMax 的余量
MSR_PERF_STATUS  = 0x198   # IA32_PERF_STATUS, bits 15:8 = 当前倍频(x100MHz)

# ★2026-10-20 本机全档位实测标定(手册 scripts/风扇档位-20261020-075530.txt):
#   0x01=2471  0x02=2858  0x03=3181  0x04=3186  0x05=3167
#   0x06=3456(低温)~3861(高温)  0x07=3858(≈6档)  0x40=4807(固定, 20~25 秒到位)
#   ★0x03/0x04/0x05 三档挤在 ~3180, 当不了中间档; 0x07 和 0x06 只差 ~400 转。
#   ★本机原生自动(0x80)天花板 ≈3900, 到不了 7 档 —— 所以不要指望"交还 EC"能压住温度。
LEVEL_RPM = {0: 0, 1: 2471, 2: 2858, 3: 3181, 4: 3186,
             5: 3167, 6: 3600, 7: 3858, 64: 4807}

DEFAULT_CONFIG = {
    "cycle_sec": 5,          # ★5 秒(原 2 秒): EC 是慢总线, 与 CPU 工具共用, 别太勤
    "hysteresis": 3,
    "failsafe_temp": 88,     # EC 0x78 达到该温度无条件拉满速 0x40
    # ---- DTS 快速通道 (MSR 0x1B1, 不占 EC 总线) ----
    "dts_max_c": 92.0,       # DTS >= 该值立即拉满 (0=关闭)
    # ---- 负载前馈 (GetSystemTimes, 免驱动) ----
    "load_boost_pct": 70.0,  # 占用 >= 该值
    "load_boost_s": 10.0,    #   持续该秒数 -> 拉满
    "load_boost_level": 64,  #   前馈目标档位 (0=关闭前馈)
    "load_release_pct": 55.0,   # 占用回落到该值以下
    "load_release_s": 10.0,     #   持续该秒数才解除前馈
    # ---- 降档保护 ----
    "dwell_s": 25,           # 换档后 N 秒内不降档(防边界横跳); 0=不限制
    "mode": "smart",         # smart(智能) | manual(手动) | max(最强散热) | auto(EC自动,备用)
    "manual_level": 6,       # 手动模式档位 (0-7, 64=满速)
    # 曲线刻度 = EC 0x78(所有实测数据都在这个刻度上; DTS 只走快速通道, 不进曲线)
    #   <45°C -> 1档(2471)  : 最低档用 0x01 不用 0x00, 停转后遇突发要从零起步
    #   55°C -> 2档(2858)
    #   65°C -> 3档(3181)
    #   75°C -> 6档(3456~3861, 固件随温度加力)
    #   79°C -> 7档(3858)   : 84°C 拉满之前的缓冲档
    #   84°C -> 满速(4807)  : 0x40 要 20~25 秒才到位, 所以阈值必须靠前
    "curve": [[45, 1], [55, 2], [65, 3], [75, 6], [79, 7], [84, 64]],
}


# ==========================================================================
# PawnIO 访问层 (一个实例 = 一个模块; 见文件头说明, 不能共用一个句柄)
# ==========================================================================
class PawnIO(object):
    def __init__(self, blob_path=None, label="LpcACPIEC"):
        blob_path = blob_path or BLOB_PATH
        self.label = label
        if not os.path.isfile(DLL_PATH):
            raise RuntimeError("找不到 PawnIOLib.dll: %s (请确认 PawnIO 驱动已安装)" % DLL_PATH)
        if not os.path.isfile(blob_path):
            raise RuntimeError("找不到 %s: %s" % (os.path.basename(blob_path), blob_path))

        self.lib = ctypes.WinDLL(DLL_PATH)
        L = self.lib
        L.pawnio_open.restype = ctypes.c_int32
        L.pawnio_open.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        L.pawnio_load.restype = ctypes.c_int32
        L.pawnio_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        L.pawnio_execute.restype = ctypes.c_int32
        L.pawnio_execute.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)]
        L.pawnio_close.restype = ctypes.c_int32
        L.pawnio_close.argtypes = [ctypes.c_void_p]

        self.handle = ctypes.c_void_p()
        hr = L.pawnio_open(ctypes.byref(self.handle))
        if hr < 0 or not self.handle.value:
            raise RuntimeError("pawnio_open 失败 (0x%08X) —— 请以管理员身份运行" % (hr & 0xFFFFFFFF))

        blob = open(blob_path, "rb").read()
        hr = L.pawnio_load(self.handle, blob, len(blob))
        if hr < 0:
            self.close()
            raise RuntimeError("pawnio_load(%s) 失败 (0x%08X)" % (label, hr & 0xFFFFFFFF))

        self.mutex = (ctypes.windll.kernel32.CreateMutexW(None, False, u"Global\\Access_EC")
                      if label == "LpcACPIEC" else None)

    def _lock(self):
        if self.mutex:
            ctypes.windll.kernel32.WaitForSingleObject(self.mutex, 2000)

    def _unlock(self):
        if self.mutex:
            ctypes.windll.kernel32.ReleaseMutex(self.mutex)

    def execute(self, name, in_list, out_count):
        arr_in = (ctypes.c_uint64 * len(in_list))(*in_list) if in_list else None
        arr_out = (ctypes.c_uint64 * out_count)() if out_count else None
        rs = ctypes.c_size_t(0)
        hr = self.lib.pawnio_execute(self.handle, name.encode("ascii"),
                                     arr_in, len(in_list), arr_out, out_count,
                                     ctypes.byref(rs))
        if hr < 0:
            raise RuntimeError("%s 失败 (0x%08X)" % (name, hr & 0xFFFFFFFF))
        return [arr_out[i] for i in range(rs.value)] if (out_count and arr_out) else []

    def pio_read(self, port):
        return int(self.execute("ioctl_pio_read", [port], 1)[0]) & 0xFF

    def pio_write(self, port, value):
        self.execute("ioctl_pio_write", [port, value & 0xFF], 0)

    def read_msr(self, reg):
        """IntelMSR 模块的 ioctl_read_msr: in=[msr], out=[value]."""
        return int(self.execute("ioctl_read_msr", [reg & 0xFFFFFFFF], 1)[0]) & 0xFFFFFFFFFFFFFFFF

    def _wait(self, want_set, want_clear, timeout_ms=1000):
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            st = self.pio_read(CTRL_PORT)
            if (st & want_set) == want_set and (st & want_clear) == 0:
                return True
            time.sleep(0.002)
        return False

    def _drain(self):
        for _ in range(10):
            st = self.pio_read(CTRL_PORT)
            if not (st & FLAG_OBF):
                break
            self.pio_read(DATA_PORT)
            time.sleep(0.001)

    def ec_read(self, offset):
        self._lock()
        try:
            self._drain()
            if not self._wait(0, FLAG_IBF | FLAG_OBF):
                raise RuntimeError("EC 状态超时 (读 0x%02X)" % offset)
            self.pio_write(CTRL_PORT, CMD_READ)
            if not self._wait(0, FLAG_IBF):
                raise RuntimeError("EC IBF 超时 (读 0x%02X)" % offset)
            self.pio_write(DATA_PORT, offset)
            if not self._wait(0, FLAG_IBF):
                raise RuntimeError("EC IBF 超时 (读 0x%02X)" % offset)
            return self.pio_read(DATA_PORT)
        finally:
            self._unlock()

    def ec_write(self, offset, value):
        self._lock()
        try:
            self._drain()
            if not self._wait(0, FLAG_IBF | FLAG_OBF):
                raise RuntimeError("EC 状态超时 (写 0x%02X)" % offset)
            self.pio_write(CTRL_PORT, CMD_WRITE)
            if not self._wait(0, FLAG_IBF):
                raise RuntimeError("EC IBF 超时 (写 0x%02X)" % offset)
            self.pio_write(DATA_PORT, offset)
            if not self._wait(0, FLAG_IBF):
                raise RuntimeError("EC IBF 超时 (写 0x%02X)" % offset)
            self.pio_write(DATA_PORT, value & 0xFF)
            if not self._wait(0, FLAG_IBF):
                raise RuntimeError("EC IBF 超时 (写 0x%02X)" % offset)
            return True
        finally:
            self._unlock()

    def set_fan(self, level):
        # ★2026-10-20: 删掉 "先写 0x31=0"。0x31 不是 FANSW, 写它会清 VPON/VRST,
        #   而且实测转速完全不变(写/不写 3888 vs 3889 RPM)。见手册 03 §5.1。
        self.ec_write(EC_FAN, level & 0xFF)

    def rpm(self):
        lo = self.ec_read(EC_RPM_LO)     # 先读 0x84 再读 0x85, 顺序不能反
        hi = self.ec_read(EC_RPM_HI)
        return (hi << 8) | lo

    def close(self):
        try:
            if self.handle and self.handle.value:
                self.lib.pawnio_close(self.handle)
                self.handle = ctypes.c_void_p()
        except Exception:
            pass


# ==========================================================================
# DTS 快速通道 (MSR 0x1B1 + 0x1A2) —— 独立 PawnIO 实例, 不占 EC 总线
# ==========================================================================
class MSRThermal(object):
    """读 CPU 封装温度 (DTS) 与实时倍频.

    温度 = TjMax - (0x1B1 >> 16 & 0x7F)。0x1B1 存的是"距 TjMax 的余量",
    直接当摄氏度读会得到 0°C 之类的荒谬值(早期脚本踩过)。
    ★注意: 满载时这个寄存器的读数会偏保守(有时反而走低), 所以它只当
      "提前量"用; 真正的兜底仍然是 EC 0x78 的 88°C 失效保护。
    """

    def __init__(self):
        self.io = PawnIO(BLOB_MSR, "IntelMSR")
        self.tjmax = self._read_tjmax()

    def _read_tjmax(self):
        try:
            v = self.io.read_msr(MSR_TJMAX)
            t = (v >> 16) & 0xFF
            return t if 50 <= t <= 120 else 100
        except Exception:
            return 100

    def read(self):
        """返回 dict(temp, thermal_status, ratio); 失败返回 None."""
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
            return {"temp": temp, "thermal_status": v & 1, "ratio": ratio}
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
def decide_level(temp, curve, current, hysteresis):
    """curve: [[temp, hfsp], ...] 按温度升序. 返回目标 HFSP 值.
    升档立即; 降档需温度低于当前档触发点 - hysteresis (防抖动)."""
    if not curve:
        return current
    target = curve[0][1]
    for t, lv in curve:
        if temp >= t:
            target = lv
    if target > current or target == current:
        return target
    # 降档: 找到当前档位对应的温度点
    tcur = None
    for t, lv in curve:
        if lv == current:
            tcur = t
    if tcur is not None and temp >= tcur - hysteresis:
        return current
    return target


# ==========================================================================
# 控制器
# ==========================================================================
class FanController(object):
    def __init__(self, console=False):
        self.console = console
        self.log = self._make_logger()
        self.cfg = self._load_config()
        self.lock = threading.Lock()
        self.ec = None
        self.stop_evt = threading.Event()
        self.exit_action = "keep"       # 退出永远保持当前档位 (EC 自动已失效, 不交还)
        # 运行状态
        self.mode = self.cfg.get("mode", "smart")
        self.manual_level = int(self.cfg.get("manual_level", 6))
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
        self.last_dts_text = "--"       # 给托盘用
        self.last_load = None           # 系统占用 %
        self.ff = LoadFeedforward()
        self.ff_active = False
        self._dts_latch = False         # DTS 是否处于"已过热"锁存(带 4°C 回差)
        self._last_change = 0.0         # 上次换档的时间戳(用于 dwell)
        self._last_reason = ""          # 上次拉满的原因(给日志/托盘)

    # ---- config ----------------------------------------------------------
    def _load_config(self):
        try:
            with open(CONF_PATH, "r", encoding="utf-8") as fh:
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
    def _make_logger(self):
        lg = logging.getLogger("fancontrol")
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            from logging.handlers import RotatingFileHandler
            # 单文件最大 3MB, 超出自动轮转为 .log.1 (共最多占 6MB, 永不无限增长)
            fh = RotatingFileHandler(LOG_PATH, maxBytes=3 * 1024 * 1024,
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
        self.ec = PawnIO()
        self.say("EC 通道就绪 (PawnIO, LpcACPIEC %d 字节)" % os.path.getsize(BLOB_PATH))
        # DTS 通道: 独立句柄 + 独立模块, 打不开也不影响风扇主功能
        try:
            self.msr = MSRThermal()
            t = self.msr.read()
            self.say("DTS 通道就绪 (IntelMSR, TjMax=%d) 当前 DTS=%s"
                     % (self.msr.tjmax,
                        ("%d°C" % t["temp"]) if t else "读不到"))
        except Exception as exc:
            self.msr = None
            self.say("DTS 通道不可用 (%s) —— 只靠 EC 温度与负载前馈" % exc)

    def _safe_snapshot(self):
        temp = self.ec.ec_read(EC_CPU_TMP)
        # ★2026-10-20: 转速只每 6 轮(约 30 秒)读一次 —— 托盘显示不需要 5 秒级精度,
        #   而每次读转速是 2 笔 EC 事务, 这台机器上 EC 还要和 CPU 功耗墙工具共用。
        #   顺带核对 HFSP: 本机固件 OWAK() 里有 If(SCRM) HFSP=0x07, 唤醒后会抢回档位,
        #   不核对的话工具会一直以为还是原来那档, 再也不重写。
        self._tick = getattr(self, "_tick", 0) + 1
        if self._tick % 6 == 1 or self.last_rpm is None:
            rpm = self.ec.rpm()
            try:
                real = self.ec.ec_read(EC_FAN)
                if real is not None and self.current_hfsp is not None and real != self.current_hfsp:
                    self.say("注意: HFSP 被外部改成 0x%02X (本工具以为是 0x%02X), 已同步"
                             % (real, self.current_hfsp))
                    self.current_hfsp = real
            except Exception:
                pass
        else:
            rpm = self.last_rpm
        return temp, rpm

    # ---- control loop --------------------------------------------------------
    def run(self):
        try:
            self._ec_init()
        except Exception as exc:
            self.say("FATAL: EC 初始化失败: %s" % exc)
            self.say("请通过『启动风扇控制.bat』以管理员身份启动。详见 fan_control.log")
            return
        cycle = max(1, int(self.cfg.get("cycle_sec", 5)))
        hyst = int(self.cfg.get("hysteresis", 3))
        fs_temp = int(self.cfg.get("failsafe_temp", 88))
        dts_max = float(self.cfg.get("dts_max_c", 92.0) or 0)
        boost_pct = float(self.cfg.get("load_boost_pct", 70.0))
        boost_s = float(self.cfg.get("load_boost_s", 10.0))
        boost_lv = int(self.cfg.get("load_boost_level", 64) or 0)
        rel_pct = float(self.cfg.get("load_release_pct", 55.0))
        rel_s = float(self.cfg.get("load_release_s", 10.0))
        dwell = float(self.cfg.get("dwell_s", 25) or 0)
        curve = [[int(t), int(v)] for t, v in self.cfg.get("curve", DEFAULT_CONFIG["curve"])]
        curve.sort(key=lambda x: x[0])
        self.say("启动模式: %s   曲线: %s" % (self.mode, curve))
        self.say("快速通道: DTS %s | 负载 %s" % (
            (">=%g°C 拉满" % dts_max) if (dts_max > 0 and self.msr) else "关闭",
            ("占用>=%g%% 持续 %gs -> 拉满" % (boost_pct, boost_s)) if boost_lv else "关闭"))

        err_count = 0
        while not self.stop_evt.is_set():
            try:
                temp, rpm = self._safe_snapshot()
                err_count = 0
                self.last_err = ""
            except Exception as exc:
                err_count += 1
                self.last_err = str(exc)
                if err_count % 5 == 1:
                    self.say("EC 读取异常 x%d: %s" % (err_count, exc))
                self.stop_evt.wait(cycle)
                continue

            # ---- 快速通道采样: DTS(MSR, 不占 EC 总线) + 系统占用 ----
            dts = dts_status = None
            if self.msr is not None:
                r = self.msr.read()
                if r:
                    dts, dts_status = r["temp"], r["thermal_status"]
            load = cpu_load_pct()
            now = time.time()
            ff = self.ff.update(load, now, boost_pct, boost_s, rel_pct, rel_s)
            # DTS 自带 4°C 回差, 免得在阈值上 64<->7 来回切
            if dts is not None and dts_max > 0:
                if dts >= dts_max:
                    self._dts_latch = True
                elif dts <= dts_max - 4:
                    self._dts_latch = False
            # 固件热状态位(0x1B1 bit0)只在温度确实高时才认 —— 本机固件有"锁存位"
            # 的先例(EC 0x89), 不设温度门的话一个卡住的位会把风扇永久钉在满速。
            bit_hot = bool(dts_status) and dts is not None and dts_max > 0 and dts >= dts_max - 8
            with self.lock:
                self.last_temp, self.last_rpm = temp, rpm
                self.last_dts, self.last_load, self.ff_active = dts, load, ff
                mode = self.mode

            try:
                if mode == "smart":
                    cur = self.current_hfsp
                    if cur is None:
                        cur = self._read_hfsp()
                    target = decide_level(temp, curve, cur, hyst)
                    reason = ""
                    # 优先级: EC 失效保护 > DTS > 固件热状态位 > 负载前馈
                    if temp >= fs_temp:
                        target, reason = 64, "EC %d°C>=%d°C" % (temp, fs_temp)
                    elif self._dts_latch:
                        target, reason = 64, "DTS %s°C" % (dts if dts is not None else "--")
                    elif bit_hot:
                        target, reason = 64, "固件热状态位"
                    elif ff and boost_lv:
                        target, reason = boost_lv, "负载 %.0f%%" % (load or 0)
                    elif (self.current_hfsp in LEVEL_RPM and target < self.current_hfsp
                          and dwell > 0 and (now - self._last_change) < dwell):
                        target = self.current_hfsp      # 驻留期内不降档
                    if target != self.current_hfsp:
                        self.ec.set_fan(target)
                        self.current_hfsp = target
                        self._last_change = now
                        self._last_reason = reason
                        self.say("%s  CPU=%dC%s  RPM=%d  -> 档位 0x%02X (%s RPM)%s"
                                 % (time.strftime("%H:%M:%S"), temp,
                                    (" DTS=%dC" % dts) if dts is not None else "",
                                    rpm, target, LEVEL_RPM.get(target, "?"),
                                    ("   [" + reason + "]") if reason else ""))
                    # 无变化不写日志 (保护 SSD; 状态随时看托盘悬停)
                elif mode == "manual":
                    manual = self.manual_level
                    hot = temp >= fs_temp or self._dts_latch or bit_hot
                    target = 64 if hot else manual
                    if hot and manual != 64:
                        self.say("!! 失效保护: CPU %dC / DTS %sC, 手动档临时拉满速"
                                 % (temp, dts if dts is not None else "--"))
                    if target != self.current_hfsp:
                        self.ec.set_fan(target)
                        self.current_hfsp = target
                        self._last_change = now
                        self.say("%s  手动模式: 档位 %s (0x%02X, %s RPM)"
                                 % (time.strftime("%H:%M:%S"),
                                    "满速" if manual == 64 else manual,
                                    target, LEVEL_RPM.get(target, "?")))
                elif mode == "max":
                    if self.current_hfsp != 64:
                        self.ec.set_fan(64)
                        self.current_hfsp = 64
                        self._last_change = now
                        self.say("%s  最强散热: 满速 0x40 (%s RPM)"
                                 % (time.strftime("%H:%M:%S"),
                                    LEVEL_RPM.get(64, "?")))
                elif mode == "auto":
                    # EC 自动 (0x80 已在切换时写入): 不干预风扇, 只读数显示
                    pass
            except Exception as exc:
                self.last_err = str(exc)
                self.say("EC 写入异常: %s" % exc)
            self.stop_evt.wait(cycle)

        # 收尾
        try:
            self.say("退出, 保持当前档位 0x%02X" % (self.current_hfsp or 0))
        except Exception as exc:
            self.say("收尾写入失败: %s" % exc)
        finally:
            try:
                self.ec.close()
            except Exception:
                pass
            if self.msr is not None:
                self.msr.close()

    def _read_hfsp(self):
        try:
            v = self.ec.ec_read(EC_FAN)
            self.current_hfsp = v
            return v
        except Exception:
            return 0


# ==========================================================================
# 托盘图标 (pystray + Pillow) -- 风扇造型
#   生效: 绿=智能  橙=手动  红=最强散热  蓝=EC自动
#   未生效 (EC 通信失败): 灰色
# ==========================================================================
def _draw_fan(color, alpha=255):
    """画一个三叶风扇图标 (64x64 RGBA)."""
    import math
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = size / 2.0
    col = (color[0], color[1], color[2], alpha)
    dark = (max(color[0] - 90, 0), max(color[1] - 90, 0), max(color[2] - 90, 0), alpha)

    # 三片扇叶 (水滴形, 间隔 120 度)
    blade = [(0, -25), (9, -17), (11, -7), (4, -3), (-5, -4), (-11, -13)]
    for i in range(3):
        ang = math.radians(i * 120 - 90)
        ca, sa = math.cos(ang), math.sin(ang)
        pts = [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in blade]
        d.polygon(pts, fill=col)

    # 中心轮毂
    d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=(250, 250, 250, alpha),
              outline=dark, width=2)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=dark)

    # 外圈
    d.ellipse([1, 1, size - 2, size - 2], outline=(90, 90, 90, alpha), width=2)
    return img


def make_icon_image(ctrl):
    if ctrl.last_err:                      # 未生效 -> 灰色
        return _draw_fan((160, 160, 160))
    if ctrl.mode == "max":
        return _draw_fan((225, 55, 55))    # 最强散热 -> 红
    if ctrl.mode == "manual":
        return _draw_fan((240, 150, 40))   # 手动模式 -> 橙
    if ctrl.mode == "auto":
        return _draw_fan((70, 130, 235))   # EC 自动 -> 蓝
    return _draw_fan((60, 185, 85))        # 智能模式 -> 绿


def run_tray(ctrl):
    import pystray

    def act_smart(icon, item):
        ctrl.set_mode("smart")

    def act_max(icon, item):
        ctrl.set_mode("max")

    def act_auto(icon, item):
        try:
            ctrl.ec.set_fan(0x80)
            ctrl.current_hfsp = 0x80
            ctrl.say("已写 0x80, 风扇交还 EC 自动")
        except Exception as exc:
            ctrl.say("写 0x80 失败: %s" % exc)
        ctrl.set_mode("auto")

    def make_manual(lv):
        def act(icon, item):
            ctrl.set_mode("manual", lv)
        return act

    def manual_text(lv):
        if lv == 0:
            return "停转 (0 RPM)"
        if lv == 64:
            return "满速 0x40 (~4810 RPM, 需 20~25 秒到位)"
        return "%d 档 (~%s RPM)" % (lv, LEVEL_RPM.get(lv, "?"))

    def act_exit(icon, item):
        ctrl.request_stop("keep")
        icon.stop()

    def act_autostart(icon, item):
        on = autostart_enabled()
        ok, _ = autostart_set(not on)
        ctrl.say("开机自启 -> %s%s" % ("开启" if not on else "关闭",
                  "" if ok else " (失败, 需要管理员权限)"))
        try:
            icon.update_menu()
        except Exception:
            pass

    def checked(mode):
        return lambda item: ctrl.mode == mode

    def manual_checked(lv):
        return lambda item: ctrl.mode == "manual" and ctrl.manual_level == lv

    menu = pystray.Menu(
        pystray.MenuItem("智能模式", act_smart, radio=True, checked=checked("smart")),
        pystray.MenuItem("手动模式",
                         pystray.Menu(*[pystray.MenuItem(
                             manual_text(lv), make_manual(lv),
                             radio=True, checked=manual_checked(lv))
                             for lv in [0, 1, 2, 6, 7, 64]]),
                         radio=True, checked=checked("manual")),
        pystray.MenuItem("最强散热", act_max, radio=True, checked=checked("max")),
        pystray.MenuItem("EC 自动 (备用)", act_auto, radio=True, checked=checked("auto")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", act_autostart,
                         checked=lambda item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: "状态: %s%s" % (_mode_text(ctrl),
                         "  [未生效!]" if ctrl.last_err else ""),
                         lambda icon, item: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 (保持当前状态)", act_exit),
    )

    icon = pystray.Icon("FanControl", _draw_fan((60, 185, 85)),
                        "FanControl 启动中...", menu)
    threading.Thread(target=tray_updater, args=(ctrl, icon), daemon=True).start()
    icon.run_detached()


def _mode_text(ctrl):
    if ctrl.mode == "smart":
        return "智能模式"
    if ctrl.mode == "manual":
        lv = ctrl.manual_level
        return "手动·满速" if lv == 64 else ("手动·停转" if lv == 0 else "手动·%d 档" % lv)
    if ctrl.mode == "max":
        return "最强散热"
    if ctrl.mode == "auto":
        return "EC 自动"
    return ctrl.mode


def tray_updater(ctrl, icon):
    """控制线程里定期刷新托盘 tooltip 和图标"""
    while not ctrl.stop_evt.is_set():
        try:
            temp, rpm = ctrl.last_temp, ctrl.last_rpm
            warn = "  ⚠未生效" if ctrl.last_err else ""
            extra = ""
            if ctrl.last_dts is not None:
                extra += "  DTS %d°C" % ctrl.last_dts
            if ctrl.last_load is not None:
                extra += "  负载 %.0f%%" % ctrl.last_load
            if ctrl.ff_active:
                extra += " [前馈]"
            icon.title = "FanControl %s%s  CPU %s%s  %s RPM" % (
                _mode_text(ctrl), warn,
                ("%d°C" % temp) if temp is not None else "--",
                extra,
                rpm if rpm is not None else "--")
            icon.icon = make_icon_image(ctrl)
        except Exception:
            pass
        ctrl.stop_evt.wait(2)


# ==========================================================================
# 开机自启 (计划任务 FanControl-AutoStart, 登录延迟 15 秒, 最高权限)
# ==========================================================================
def _schtasks(args):
    """运行 schtasks, 返回 (返回码, 输出文本). XML 输出为 UTF-16, 其余忽略即可."""
    try:
        p = subprocess.run(["schtasks"] + args, capture_output=True)
        try:
            out = p.stdout.decode("utf-16", "ignore") if b"\x00" in (p.stdout or b"")[:4] \
                else p.stdout.decode("gbk", "ignore")
        except Exception:
            out = ""
        return p.returncode, out
    except Exception as exc:
        return -1, str(exc)


def autostart_task_command():
    """返回自启任务的 Command 路径 (未去引号原样); 任务不存在返回 None."""
    rc, out = _schtasks(["/Query", "/TN", TASK_NAME, "/XML"])
    if rc != 0:
        return None
    m = re.search(r"<Command>(.*?)</Command>", out)
    return m.group(1) if m else ""


def autostart_enabled():
    """自启任务存在且指向当前 exe 才算已开启."""
    if not EXE_FOR_TASK:
        return False
    cmd = autostart_task_command()
    if not cmd:
        return False
    try:
        return os.path.normpath(os.path.abspath(cmd.strip().strip('"'))) \
            == os.path.normpath(EXE_FOR_TASK)
    except Exception:
        return False


def _task_settings_fix():
    """把计划的电池两条限制改成 false.

    schtasks /Create 一定写死 DisallowStartIfOnBatteries=true /
    StopIfGoingOnBatteries=true —— 后果是**用电池开机时风扇工具根本不会启动**,
    而笔记本恰恰最需要不插电时也有风扇控制。schtasks 命令行没有对应开关,
    只能用 PowerShell 的 ScheduledTasks 模块改。
    """
    ps = ("$n='%s'; try{ $t=Get-ScheduledTask -TaskName $n -ErrorAction Stop;"
          "  $t.Settings.DisallowStartIfOnBatteries=$false;"
          "  $t.Settings.StopIfGoingOnBatteries=$false;"
          "  $t.Settings.ExecutionTimeLimit='PT0S';"
          "  Set-ScheduledTask -TaskName $n -Settings $t.Settings | Out-Null;"
          "  exit 0 } catch { exit 1 }" % TASK_NAME)
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, creationflags=0x08000000)
        return p.returncode == 0
    except Exception:
        return False


def _task_allows_battery():
    """任务是否没被"电池就不跑"挡住; 查不到当没挡(不乱动)."""
    rc, out = _schtasks(["/Query", "/TN", TASK_NAME, "/XML"])
    if rc != 0:
        return True
    m = re.search(r"<DisallowStartIfOnBatteries>\s*(true|false)\s*</DisallowStartIfOnBatteries>",
                  out, re.I)
    return not (m and m.group(1).lower() == "true")


def autostart_set(enable):
    """开启/关闭开机自启. 需要管理员权限 (程序本身已提权)."""
    if not EXE_FOR_TASK:
        return False, "无法定位 FanControl.exe"
    if enable:
        rc, out = _schtasks(["/Create", "/F", "/TN", TASK_NAME,
                             "/TR", '"%s"' % EXE_FOR_TASK,
                             "/SC", "ONLOGON", "/DELAY", "0000:15",
                             "/RL", "HIGHEST"])
        if rc != 0:
            return False, out
        ok2 = _task_settings_fix()
        return True, out + ("" if ok2
                            else " [警告] 电池设置没改成功: 用电池开机时不会自启")
    rc, out = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    return rc == 0, out


def autostart_autofix(ctrl):
    """启动时自查自启任务: 指向旧路径 / 被"电池不跑"挡住 就顺手修正."""
    if not EXE_FOR_TASK:
        return
    try:
        cmd = autostart_task_command()
        if cmd is None:
            return
        if not autostart_enabled():
            ok, _ = autostart_set(True)
            ctrl.say("开机自启路径已修正 -> %s%s"
                     % (EXE_FOR_TASK, "" if ok else " (修正失败)"))
        elif not _task_allows_battery():
            ok = _task_settings_fix()
            ctrl.say("开机自启已解除电池限制%s"
                     % ("" if ok else " (失败, 需管理员)"))
    except Exception:
        pass


# ==========================================================================
def check_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, True, u"FanControl_T14s_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ==========================================================================
def selftest():
    """离线自测: 曲线/滞回、负载前馈状态机、DTS 换算。不碰 EC / MSR。"""
    fails = []

    def check(name, got, want):
        ok = (got == want)
        if not ok:
            fails.append(name)
        print("  [%s] %-34s got=%-5s want=%s" % ("OK" if ok else "!!", name, got, want))

    print("== 1. 曲线决策 (EC 0x78 刻度, 滞回 3) ==")
    curve = DEFAULT_CONFIG["curve"]
    cur = 1
    for temp in [30, 44, 45, 54, 55, 64, 65, 74, 75, 78, 79, 83, 84, 90,
                 83, 81, 80, 78, 76, 75, 70, 65, 60, 50, 40]:
        cur = decide_level(temp, curve, cur, 3)
        print("  %2dC -> 0x%02X (%s RPM)" % (temp, cur, LEVEL_RPM.get(cur, "?")))
    check("最低档是 0x01 不是停转", decide_level(30, curve, 1, 3), 1)
    check("45C -> 1 档", decide_level(45, curve, 1, 3), 1)
    check("55C -> 2 档", decide_level(55, curve, 1, 3), 2)
    check("65C -> 3 档", decide_level(65, curve, 1, 3), 3)
    check("75C -> 6 档", decide_level(75, curve, 3, 3), 6)
    check("79C -> 7 档", decide_level(79, curve, 6, 3), 7)
    check("84C -> 满速", decide_level(84, curve, 7, 3), 64)
    check("滞回: 81C 仍满速", decide_level(81, curve, 64, 3), 64)
    check("滞回: 80C 退到 7 档", decide_level(80, curve, 64, 3), 7)

    print("== 2. 负载前馈状态机 (70% / 10s 进, 55% / 10s 出) ==")
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

    print("== 3. DTS 换算 (TjMax - 余量) ==")
    def dts_temp(tjmax, raw):
        return tjmax - ((raw >> 16) & 0x7F)
    check("TjMax=100 余量=8  -> 92°C", dts_temp(100, 8 << 16), 92)
    check("TjMax=100 余量=20 -> 80°C", dts_temp(100, 20 << 16), 80)
    check("余量 0 -> 100°C (TjMax)", dts_temp(100, 0), 100)

    print("")
    if fails:
        print("失败 %d 项: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("全部通过。")
    return 0


def main():
    console = "--console" in sys.argv
    if "--selftest" in sys.argv:
        return selftest()

    if not check_single_instance():
        print("FanControl 已在运行 (托盘图标), 本次退出。")
        return 0

    ctrl = FanController(console=console)
    if not check_admin():
        ctrl.say("警告: 未以管理员身份运行, PawnIO 可能打不开 EC!")

    t = threading.Thread(target=ctrl.run, daemon=True)
    t.start()

    try:
        run_tray(ctrl)
        # pystray run_detached 需要 mainloop 由我们驱动
        import pystray
        # run_detached 已自带线程, 这里阻塞等待控制线程结束
        while t.is_alive():
            time.sleep(0.5)
    except ImportError:
        # 没有 pystray: 控制台模式兜底
        ctrl.say("pystray 不可用, 使用纯控制台模式 (Ctrl+C 退出)")
        try:
            while t.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            ctrl.request_stop("keep")
    except Exception as exc:
        ctrl.say("托盘初始化失败(%s), 转控制台模式" % exc)
        try:
            while t.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            ctrl.request_stop("keep")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FanControl -- ThinkPad T14s Gen 4 智能风扇控制 (PawnIO EC 直连)
重写自 TPFanCtrl2 fork 的核心功能, 修复其两个缺陷:
  1) 启动时按配置文件恢复模式 (Smart/手动), 不再每次都回到 EC 自动
  2) 界面语言/模式等任何变更立即持久化到 config.json

用法:
  pythonw fan_control.py            常规启动 (托盘图标)
  python  fan_control.py --console  带控制台日志启动
  python  fan_control.py --selftest 只测曲线决策逻辑, 不碰 EC
"""

import ctypes
import json
import logging
import os
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # PyInstaller 打包后: 配置/日志放 exe 旁边, 资源从解包目录取
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    BLOB_PATH = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "LpcACPIEC.bin")
else:
    APP_DIR = SCRIPT_DIR
    BLOB_PATH = os.path.join(SCRIPT_DIR, "LpcACPIEC.bin")
LOG_PATH   = os.path.join(APP_DIR, "fan_control.log")
CONF_PATH  = os.path.join(APP_DIR, "config.json")
DLL_PATH   = r"C:\Program Files\PawnIO\PawnIOLib.dll"

# ---- EC 寄存器 (与本机实测一致) ------------------------------------------
CTRL_PORT, DATA_PORT = 0x66, 0x62
FLAG_OBF, FLAG_IBF   = 0x01, 0x02
CMD_READ, CMD_WRITE  = 0x80, 0x81
EC_FAN     = 0x2F   # HFSP
EC_FANSW   = 0x31   # fan select
EC_RPM_LO  = 0x84
EC_RPM_HI  = 0x85
EC_CPU_TMP = 0x78

LEVEL_RPM = {0: 0, 1: 2029, 2: 2362, 3: 2600, 4: 2964,
             5: 3185, 6: 3170, 7: 3924, 64: 4946}   # 本机实测中位转速

DEFAULT_CONFIG = {
    "cycle_sec": 2,
    "hysteresis": 3,
    "failsafe_temp": 88,     # CPU 达到该温度无条件拉满速 0x40
    "mode": "smart",         # smart(智能) | manual(手动) | max(最强散热) | auto(EC自动,固件修复后用)
    "manual_level": 5,       # 手动模式档位 (0-7, 64=满速)
    "curve": [[38, 0], [40, 2], [48, 3], [55, 4],
              [62, 5], [68, 6], [74, 7], [84, 64]],
}


# ==========================================================================
# PawnIO / EC 访问层 (移植自实测通过的 ec_direct.py)
# ==========================================================================
class PawnIO(object):
    def __init__(self):
        if not os.path.isfile(DLL_PATH):
            raise RuntimeError("找不到 PawnIOLib.dll: %s (请确认 PawnIO 驱动已安装)" % DLL_PATH)
        if not os.path.isfile(BLOB_PATH):
            raise RuntimeError("找不到 LpcACPIEC.bin: %s" % BLOB_PATH)

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

        blob = open(BLOB_PATH, "rb").read()
        hr = L.pawnio_load(self.handle, blob, len(blob))
        if hr < 0:
            self.close()
            raise RuntimeError("pawnio_load(LpcACPIEC) 失败 (0x%08X)" % (hr & 0xFFFFFFFF))

        self.mutex = ctypes.windll.kernel32.CreateMutexW(None, False, u"Global\\Access_EC")

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
        self.ec_write(EC_FANSW, 0x00)
        self.ec_write(EC_FAN, level & 0xFF)

    def rpm(self):
        self.ec_write(EC_FANSW, 0x00)
        lo = self.ec_read(EC_RPM_LO)
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
        self.manual_level = int(self.cfg.get("manual_level", 5))
        if self.mode not in ("smart", "manual", "max", "auto"):
            self.mode = "manual" if self.mode == "silent" else "smart"
            self.cfg["mode"] = self.mode
        self.current_hfsp = None
        self.last_temp = None
        self.last_rpm = None
        self.last_err = ""

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

    # ---- EC ----------------------------------------------------------------
    def _ec_init(self):
        self.ec = PawnIO()
        self.say("EC 通道就绪 (PawnIO, LpcACPIEC %d 字节)" % os.path.getsize(BLOB_PATH))

    def _safe_snapshot(self):
        temp = self.ec.ec_read(EC_CPU_TMP)
        rpm = self.ec.rpm()
        return temp, rpm

    # ---- control loop --------------------------------------------------------
    def run(self):
        try:
            self._ec_init()
        except Exception as exc:
            self.say("FATAL: EC 初始化失败: %s" % exc)
            self.say("请通过『启动风扇控制.bat』以管理员身份启动。详见 fan_control.log")
            return
        cycle = max(1, int(self.cfg.get("cycle_sec", 2)))
        hyst = int(self.cfg.get("hysteresis", 3))
        fs_temp = int(self.cfg.get("failsafe_temp", 88))
        curve = [[int(t), int(v)] for t, v in self.cfg.get("curve", DEFAULT_CONFIG["curve"])]
        curve.sort(key=lambda x: x[0])
        self.say("启动模式: %s   曲线: %s" % (self.mode, curve))

        err_count = 0
        while not self.stop_evt.is_set():
            try:
                temp, rpm = self._safe_snapshot()
                with self.lock:
                    self.last_temp, self.last_rpm = temp, rpm
                    mode = self.mode
                err_count = 0
                self.last_err = ""
            except Exception as exc:
                err_count += 1
                self.last_err = str(exc)
                if err_count % 5 == 1:
                    self.say("EC 读取异常 x%d: %s" % (err_count, exc))
                self.stop_evt.wait(cycle)
                continue

            try:
                if mode == "smart":
                    cur = self.current_hfsp
                    if cur is None:
                        cur = self._read_hfsp()
                    target = decide_level(temp, curve, cur, hyst)
                    if temp >= fs_temp and target != 64:
                        target = 64
                        self.say("!! 失效保护: CPU %dC >= %dC, 强制满速 0x40" % (temp, fs_temp))
                    if target != self.current_hfsp:
                        self.ec.set_fan(target)
                        self.current_hfsp = target
                        self.say("%s  CPU=%2dC  RPM=%4d  -> 档位 0x%02X (%s RPM)"
                                 % (time.strftime("%H:%M:%S"), temp, rpm, target,
                                    LEVEL_RPM.get(target, "?")))
                    # 无变化不写日志 (保护 SSD; 状态随时看托盘悬停)
                elif mode == "manual":
                    manual = self.manual_level
                    target = 64 if temp >= fs_temp else manual
                    if temp >= fs_temp and manual != 64:
                        self.say("!! 失效保护: CPU %dC >= %dC, 手动档临时拉满速" % (temp, fs_temp))
                    if target != self.current_hfsp:
                        self.ec.set_fan(target)
                        self.current_hfsp = target
                        self.say("%s  手动模式: 档位 %s (0x%02X, %s RPM)"
                                 % (time.strftime("%H:%M:%S"),
                                    "满速" if manual == 64 else manual,
                                    target, LEVEL_RPM.get(target, "?")))
                elif mode == "max":
                    if self.current_hfsp != 64:
                        self.ec.set_fan(64)
                        self.current_hfsp = 64
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
            self.ec.close()

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
            return "满速 0x40 (~4950 RPM)"
        return "%d 档 (~%s RPM)" % (lv, LEVEL_RPM.get(lv, "?"))

    def act_exit(icon, item):
        ctrl.request_stop("keep")
        icon.stop()

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
                             for lv in [0, 1, 2, 3, 4, 5, 6, 7, 64]]),
                         radio=True, checked=checked("manual")),
        pystray.MenuItem("最强散热", act_max, radio=True, checked=checked("max")),
        pystray.MenuItem("EC 自动 (备用)", act_auto, radio=True, checked=checked("auto")),
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
            icon.title = "FanControl %s%s  CPU %s  %s RPM" % (
                _mode_text(ctrl), warn,
                ("%d°C" % temp) if temp is not None else "--",
                rpm if rpm is not None else "--")
            icon.icon = make_icon_image(ctrl)
        except Exception:
            pass
        ctrl.stop_evt.wait(2)


# ==========================================================================
def check_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, True, u"FanControl_T14s_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    console = "--console" in sys.argv
    if "--selftest" in sys.argv:
        print("== 曲线决策自测 (不碰 EC) ==")
        curve = DEFAULT_CONFIG["curve"]
        cur = 0
        for temp in [36, 39, 41, 45, 50, 55, 60, 63, 65, 70, 75, 80, 83, 85, 86, 78, 74, 71, 65, 60, 55, 50, 45, 40, 36]:
            cur = decide_level(temp, curve, cur, 3)
            print("  %2dC -> 0x%02X (%s RPM)" % (temp, cur, LEVEL_RPM.get(cur, "?")))
        return 0

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

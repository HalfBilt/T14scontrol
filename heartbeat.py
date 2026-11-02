"""断电黑匣子：每 30 秒往 logs\\heartbeat.log 追加一行（flush + fsync）。
用途：6008 里的"上次关闭时间"是 LastAliveStamp，可能早 30 分钟；这个文件最后一行
     就是**精确到秒**的断电时刻，并带上当时的交流状态/电量/温度/功耗/档位。

只读 status\\*.json + GetSystemPowerStatus，不碰 EC，不需要管理员。
单实例：占用本地端口 50999，占不到就退出（计划任务重复触发时不会跑两份）。
"""
import os
import sys
import json
import time
import socket
import ctypes
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "logs")
LOG = os.path.join(LOG_DIR, "heartbeat.log")
INTERVAL = 30
MAX_BYTES = 3 * 1024 * 1024
KEEP_LINES = 4000
LOCK_PORT = 50999


class SPS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def jread(*parts):
    try:
        with open(os.path.join(BASE, *parts), encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return {}


def rotate():
    try:
        if os.path.getsize(LOG) > MAX_BYTES:
            with open(LOG, encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-KEEP_LINES:]
            with open(LOG, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
    except Exception:
        pass


def line():
    s = SPS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    c = jread("status", "cpu.json")
    f = jread("status", "fan.json")
    return ("%s  AC=%s bat=%s%%  EC=%s DTS=%s PKG=%s load=%s  hfsp=%s PL=%s/%s freq=%s"
            % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               s.ACLineStatus, s.BatteryLifePercent,
               c.get("ec"), c.get("dts"), f.get("pkg"), f.get("load"),
               f.get("hfsp"), c.get("pl1"), c.get("pl2"), c.get("freq_mhz")))


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        srv.bind(("127.0.0.1", LOCK_PORT))
        srv.listen(1)
    except OSError:
        return 0                      # 已经有一份在跑
    os.makedirs(LOG_DIR, exist_ok=True)
    rotate()
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("# 启动 %s  (pid=%s)\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        fh.flush(); os.fsync(fh.fileno())
        while True:
            try:
                fh.write(line() + "\n")
                fh.flush(); os.fsync(fh.fileno())
            except Exception:
                pass
            time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())

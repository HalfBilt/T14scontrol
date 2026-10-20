# -*- coding: utf-8 -*-
"""FanControlSvc —— 把风扇控制做成 Windows 服务(SYSTEM 常驻, 不依赖登录)。

为什么不做托盘了:
  1. 托盘程序只在**用户登录后**运行 —— 不登录就没有风扇控制, 只剩固件原生曲线
     (实测原生天花板 ≈3890 RPM, 压不住满载, 手册 02 §1);
  2. 托盘程序是用户级进程, 拿不到 PawnIO 需要的特权, 也拿不到 'Global\\CpuPLFix-EC'
     那把锁的合适权限(服务以 SYSTEM 身份建, 用户进程可能打不开);
  3. 服务由 SCM 在开机早期拉起, 睡眠唤醒后能立刻接管风扇档位。

与 CPU 侧(CpuPLFixSvc)的关系:
  - 两者都只**读** EC 温度; 风扇侧写 0x2F, CPU 侧不写 EC;
  - 两边的 EC 端口事务共用 'Global\\CpuPLFix-EC' + 'Global\\Access_EC' 两把锁(顺序一致),
    这样不会互相插队 —— 插队会读走系统 ACPI EC 驱动正在等的字节 ->
    ACPI 事件 13 -> DSDT 的 _TMP 拿不到温度 -> 固件报 _HOT -> 强制休眠(事件 88)。

用法: 由 fan_control.py install-svc 注册
    pythonw.exe FanControlSvc.py --service
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes as w

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import importlib.util

_spec = importlib.util.spec_from_file_location('fanctl', os.path.join(BASE, 'fan_control.py'))
fan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fan)

SERVICE_NAME = 'T14sFanSvc'
SERVICE_DISPLAY = 'T14s 智能风扇控制'

SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_START_PENDING = 2
SERVICE_STOPPED = 1
SERVICE_RUNNING = 4
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_POWEREVENT = 0x00000040
SERVICE_CONTROL_STOP = 1
SERVICE_CONTROL_INTERROGATE = 4
SERVICE_CONTROL_POWEREVENT = 0x0000000D

PBT_APMRESUMECRITICAL = 0x0006
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMPOWERSTATUSCHANGE = 0x000A
RESUME_EVENTS = (PBT_APMRESUMECRITICAL, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC)
ALL_EVENTS = RESUME_EVENTS + (PBT_APMPOWERSTATUSCHANGE,)

adv = ctypes.WinDLL('advapi32', use_last_error=True)


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [('dwServiceType', w.DWORD),
                ('dwCurrentState', w.DWORD),
                ('dwControlsAccepted', w.DWORD),
                ('dwWin32ExitCode', w.DWORD),
                ('dwServiceSpecificExitCode', w.DWORD),
                ('dwCheckPoint', w.DWORD),
                ('dwWaitHint', w.DWORD)]


HANDLER = ctypes.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p, ctypes.c_void_p)
SERVICE_MAIN_FUNC = ctypes.WINFUNCTYPE(None, w.DWORD, ctypes.POINTER(w.LPWSTR))


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [('lpServiceName', w.LPWSTR),
                ('lpServiceProc', SERVICE_MAIN_FUNC)]


adv.RegisterServiceCtrlHandlerExW.restype = w.HANDLE
adv.RegisterServiceCtrlHandlerExW.argtypes = [w.LPCWSTR, HANDLER, ctypes.c_void_p]
adv.SetServiceStatus.argtypes = [w.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRY)]

_stop = threading.Event()
_wake = threading.Event()
_resume = [0]
_status_handle = None
_status = SERVICE_STATUS()
_handler_ref = None


def _set_status(state, accepted=0, hint=0, cp=0):
    global _status
    _status.dwServiceType = SERVICE_WIN32_OWN_PROCESS
    _status.dwCurrentState = state
    _status.dwControlsAccepted = accepted
    _status.dwWin32ExitCode = 0
    _status.dwServiceSpecificExitCode = 0
    _status.dwCheckPoint = cp
    _status.dwWaitHint = hint
    if _status_handle:
        adv.SetServiceStatus(_status_handle, ctypes.byref(_status))


def _handler(control, event_type, event_data, context):
    if control == SERVICE_CONTROL_STOP:
        _stop.set()
        _wake.set()
        return 0
    if control == SERVICE_CONTROL_INTERROGATE:
        _set_status(_status.dwCurrentState, SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_POWEREVENT)
        return 0
    if control == SERVICE_CONTROL_POWEREVENT:
        # 睡眠/休眠恢复/拔插电: 唤醒后固件的 OWAK() 会把 HFSP 抢回 0x07/0x80,
        # 必须 1 秒内重写档位, 否则要等 30 秒的转速轮询才纠正。
        if event_type in ALL_EVENTS:
            _resume[0] = event_type
            _wake.set()
        return 0
    return 0


def _pbt_name(t):
    if t == PBT_APMPOWERSTATUSCHANGE:
        return '电源状态变化(拔/插电)'
    return {PBT_APMRESUMECRITICAL: '休眠恢复-未通知',
            PBT_APMRESUMESUSPEND: '用户操作唤醒',
            PBT_APMRESUMEAUTOMATIC: '自动唤醒'}.get(t, '未知')


def _worker():
    # PawnIO 在服务(session 0)里能不能用, 先试; 不行就退到 console 模式的控制器
    ctrl = None
    try:
        ctrl = fan.FanController(console=True, log_name="fan_service.log")
        ctrl.say('[*] 服务启动: SYSTEM 会话, 准备接管风扇档位')
    except Exception as exc:
        import traceback
        try:
            fan.LOG_PATH  # noqa
            with open(fan.LOG_PATH, 'a', encoding='utf-8') as fh:
                fh.write('服务启动失败: %s\n%s\n' % (exc, traceback.format_exc()))
        except Exception:
            pass
        return
    try:
        ctrl.start_service_loop(_stop, _wake, _resume, _pbt_name)
    except Exception as exc:
        import traceback
        ctrl.say('服务循环异常: %s' % exc)
        ctrl.say(traceback.format_exc())
    finally:
        try:
            ctrl.request_stop('keep')
        except Exception:
            pass


def _service_main(argc, argv):
    global _status_handle, _handler_ref
    _handler_ref = HANDLER(_handler)
    _status_handle = adv.RegisterServiceCtrlHandlerExW(SERVICE_NAME, _handler_ref, None)
    _set_status(SERVICE_START_PENDING, 0, 5000, 1)
    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    _set_status(SERVICE_RUNNING, SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_POWEREVENT, 0, 0)
    _stop.wait()
    _set_status(SERVICE_STOPPED, 0, 0, 0)


def run_service():
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = SERVICE_MAIN_FUNC(_service_main)
    table[1].lpServiceName = None
    table[1].lpServiceProc = SERVICE_MAIN_FUNC()
    if not adv.StartServiceCtrlDispatcherW(table):
        raise OSError('StartServiceCtrlDispatcher 失败: %d' % ctypes.get_last_error())


if __name__ == '__main__':
    if '--service' in sys.argv:
        run_service()
    else:
        print('这是 Windows 服务入口, 请用 fan_control.py install-svc 注册。')

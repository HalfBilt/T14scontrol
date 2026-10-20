# -*- coding: utf-8 -*-
"""风扇服务的注册/卸载/状态查询(供 bat 调用)。

    python fan_service_ctl.py install     注册并启动 T14sFanSvc
    python fan_service_ctl.py uninstall   停止并删除
    python fan_service_ctl.py status      看状态
    python fan_service_ctl.py start|stop

服务跑的是 FanControlSvc.py --service, 二进制是"自带 runtime 的 pythonw.exe"。
注意: PawnIO 驱动必须已安装(风扇侧要读写 EC 0x2F), 否则服务起不来 —— 日志会写明。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes as w

BASE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(BASE)

SERVICE_NAME = 'T14sFanSvc'
SERVICE_DISPLAY = 'T14s 智能风扇控制'
SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_AUTO_START = 2
SERVICE_ERROR_NORMAL = 1
SERVICE_ALL_ACCESS = 0x000F01FF
DELETE = 0x00010000
SERVICE_START = 0x00000010
SERVICE_STOP = 0x00000020
SERVICE_QUERY_STATUS = 0x00000004
SC_MANAGER_CREATE_SERVICE = 0x00000002
SC_MANAGER_CONNECT = 0x00000001

adv = ctypes.WinDLL('advapi32', use_last_error=True)
adv.OpenSCManagerW.restype = w.HANDLE
adv.OpenSCManagerW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
adv.CreateServiceW.restype = w.HANDLE
adv.CreateServiceW.argtypes = [w.HANDLE, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                               w.DWORD, w.LPCWSTR, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID,
                               w.LPVOID]
adv.OpenServiceW.restype = w.HANDLE
adv.OpenServiceW.argtypes = [w.HANDLE, w.LPCWSTR, w.DWORD]
adv.DeleteService.argtypes = [w.HANDLE]
adv.CloseServiceHandle.argtypes = [w.HANDLE]
adv.StartServiceW.restype = w.BOOL
adv.StartServiceW.argtypes = [w.HANDLE, w.DWORD, w.LPVOID]
adv.ControlService.restype = w.BOOL
adv.ControlService.argtypes = [w.HANDLE, w.DWORD, w.LPVOID]
adv.QueryServiceStatusEx.restype = w.BOOL
adv.QueryServiceStatusEx.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, w.LPVOID]

SERVICE_STATUS_PROCESS = type('S', (ctypes.Structure,), {
    '_fields_': [('dwServiceType', w.DWORD), ('dwCurrentState', w.DWORD),
                 ('dwControlsAccepted', w.DWORD), ('dwWin32ExitCode', w.DWORD),
                 ('dwServiceSpecificExitCode', w.DWORD), ('dwCheckPoint', w.DWORD),
                 ('dwWaitHint', w.DWORD), ('dwProcessId', w.DWORD),
                 ('dwServiceFlags', w.DWORD)]})

STATES = {1: 'STOPPED', 2: 'START_PENDING', 3: 'STOP_PENDING', 4: 'RUNNING',
          5: 'CONTINUE_PENDING', 6: 'PAUSE_PENDING', 7: 'PAUSED'}


def scm():
    return adv.OpenSCManagerW(None, None, SC_MANAGER_CREATE_SERVICE | SC_MANAGER_CONNECT)


def find_runtime():
    for p in (os.path.join(APP_DIR, 'runtime', 'pythonw.exe'),
              os.path.join(APP_DIR, 'runtime', 'python.exe'),
              sys.executable):
        if p and os.path.exists(p):
            return p
    return None


def binpath():
    py = find_runtime()
    if not py:
        return None
    return '"%s" "%s" --service' % (py, os.path.join(BASE, 'FanControlSvc.py'))


def do_install():
    bp = binpath()
    if not bp:
        print('[X] 找不到 python 运行时(runtime\\python.exe)')
        return 1
    h = scm()
    if not h:
        print('[X] OpenSCManager 失败 %d(需要管理员)' % ctypes.get_last_error())
        return 1
    try:
        hs = adv.CreateServiceW(h, SERVICE_NAME, SERVICE_DISPLAY, SERVICE_ALL_ACCESS,
                                SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START,
                                SERVICE_ERROR_NORMAL, bp, None, None, None, None, None)
        if not hs:
            err = ctypes.get_last_error()
            if err == 1073:                      # 已存在 -> 先删再建
                hs = adv.OpenServiceW(h, SERVICE_NAME, DELETE | SERVICE_STOP |
                                      SERVICE_QUERY_STATUS | SERVICE_START)
                if hs:
                    adv.ControlService(hs, 1, None)      # SERVICE_CONTROL_STOP
                    time.sleep(1.0)
                    adv.DeleteService(hs)
                    adv.CloseServiceHandle(hs)
                    time.sleep(0.5)
                hs = adv.CreateServiceW(h, SERVICE_NAME, SERVICE_DISPLAY,
                                        SERVICE_ALL_ACCESS, SERVICE_WIN32_OWN_PROCESS,
                                        SERVICE_AUTO_START, SERVICE_ERROR_NORMAL, bp,
                                        None, None, None, None, None)
            if not hs:
                print('[X] CreateService 失败 %d' % ctypes.get_last_error())
                return 1
        adv.CloseServiceHandle(hs)
        print('[*] 服务已注册: %s' % SERVICE_NAME)
        print('    bin: %s' % bp)
        time.sleep(0.6)
        hs = adv.OpenServiceW(h, SERVICE_NAME, SERVICE_START | SERVICE_QUERY_STATUS)
        if hs:
            if adv.StartServiceW(hs, 0, None):
                print('[*] 服务已启动')
            else:
                print('[!] 启动失败 %d —— 看 fan_service.log(常见原因: PawnIO 没装)'
                      % ctypes.get_last_error())
            adv.CloseServiceHandle(hs)
        return 0
    finally:
        adv.CloseServiceHandle(h)


def do_uninstall():
    h = scm()
    if not h:
        print('[X] OpenSCManager 失败 %d(需要管理员)' % ctypes.get_last_error())
        return 1
    try:
        hs = adv.OpenServiceW(h, SERVICE_NAME, DELETE | SERVICE_STOP | SERVICE_QUERY_STATUS)
        if not hs:
            print('[*] 服务不存在')
            return 0
        adv.ControlService(hs, 1, None)
        time.sleep(1.0)
        adv.DeleteService(hs)
        adv.CloseServiceHandle(hs)
        print('[*] 服务已删除')
        return 0
    finally:
        adv.CloseServiceHandle(h)


def do_start():
    h = scm()
    if not h:
        return 1
    try:
        hs = adv.OpenServiceW(h, SERVICE_NAME, SERVICE_START | SERVICE_QUERY_STATUS)
        if not hs:
            print('[X] 服务未注册')
            return 1
        if adv.StartServiceW(hs, 0, None):
            print('[*] 已启动')
        else:
            print('[!] 启动失败 %d' % ctypes.get_last_error())
        adv.CloseServiceHandle(hs)
        return 0
    finally:
        adv.CloseServiceHandle(h)


def do_stop():
    h = scm()
    if not h:
        return 1
    try:
        hs = adv.OpenServiceW(h, SERVICE_NAME, SERVICE_STOP | SERVICE_QUERY_STATUS)
        if not hs:
            print('[X] 服务未注册')
            return 1
        adv.ControlService(hs, 1, None)
        print('[*] 已停止')
        adv.CloseServiceHandle(hs)
        return 0
    finally:
        adv.CloseServiceHandle(h)


def do_status():
    h = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not h:
        print('[X] OpenSCManager 失败 %d' % ctypes.get_last_error())
        return 1
    try:
        hs = adv.OpenServiceW(h, SERVICE_NAME, SERVICE_QUERY_STATUS)
        if not hs:
            print('服务: 未注册')
            return 1
        st = SERVICE_STATUS_PROCESS()
        adv.QueryServiceStatusEx(hs, 0, ctypes.byref(st), ctypes.sizeof(st), None)
        adv.CloseServiceHandle(hs)
        print('服务: %s  状态: %s  PID: %d'
              % (SERVICE_NAME, STATES.get(st.dwCurrentState, st.dwCurrentState),
                 st.dwProcessId))
        return 0
    finally:
        adv.CloseServiceHandle(h)


if __name__ == '__main__':
    act = sys.argv[1] if len(sys.argv) > 1 else 'status'
    fn = {'install': do_install, 'uninstall': do_uninstall, 'start': do_start,
          'stop': do_stop, 'status': do_status}.get(act)
    if not fn:
        print('用法: fan_service_ctl.py install|uninstall|start|stop|status')
        sys.exit(2)
    sys.exit(fn())

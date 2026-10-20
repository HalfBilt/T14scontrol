# -*- coding: utf-8 -*-
"""T14scontrol 任务栏 HUD —— 任务栏上的实时硬件读数，视觉对齐 Windows 11。

数据来源（不碰 EC/MSR，普通权限即可）
    ..\\status\\cpu.json   CpuPLFixSvc：频率 / EC 温度 / DTS / 保护档 / PL1·PL2
    ..\\status\\fan.json   T14sFanSvc ：转速 / 档位 / EC·DTS / 封装功率

渲染方式
    逐像素 alpha + UpdateLayeredWindow（不是颜色键），所以做得到
    半透明圆角底 + 抗锯齿小字，效果接近 Win11 托盘文字。

交互
    左键拖动    改位置
    右键        显示项勾选 / 尺寸（小·标准·大·特大）/ 外观（跟随系统·深色·浅色）/
                底框开关 / 开机自启 / 恢复默认位置 / 显示-隐藏 / 退出
    托盘图标    双击显示-隐藏；右键同一菜单；图标随温度变色（三叶风扇）

只用标准库 ctypes（共用运行时是 embeddable 版，没有 tkinter / Pillow）。
"""

import ctypes
import ctypes.wintypes as w
import json
import math
import os
import sys
import time

APP_NAME = 'T14scontrolHUD'
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_TIMER = 0x0113
WM_PAINT = 0x000F
WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_MOUSELEAVE = 0x02A3

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
TRANSPARENT = 1
DT_LEFT = 0x0000
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOPREFIX = 0x0800

NIF_INFO = 0x10
NIIF_WARNING = 0x02
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
MF_STRING = 0x0000
MF_CHECKED = 0x0008
MF_SEPARATOR = 0x0800

HWND_TOPMOST = -1
ABM_GETTASKBARPOS = 5
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

BASE = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.path.join(os.path.dirname(BASE), 'status')
CFG_PATH = os.path.join(BASE, 'hud.json')

FIELDS = [
    ('freq', 'CPU 频率'),
    ('temp', 'EC 温度'),
    ('dts', 'DTS 温度'),
    ('fan', '风扇转速'),
    ('gear', '风扇档位'),
    ('pkg', '封装功率'),
    ('pl', '功耗墙 PL1/PL2'),
    ('mem', '内存占用'),
    ('batt', '电池'),
    ('guard', '保护状态'),
    ('time', '时间'),
]

SCALES = [('小', 0.80), ('标准', 1.0), ('大', 1.18), ('特大', 1.38)]

DEFAULT_CFG = {
    'enabled': True,
    'fields': ['freq', 'temp', 'fan'],
    'x': None,
    'y': None,
    'scale': 1.0,
    'theme': 'auto',
    'pill': True,
    'locked': False,
    'offset_right': 470,
}

# 调色板（BGR）
PALETTE = {
    'dark': {
        'pill': (44, 44, 47, 205), 'line': (90, 90, 96, 70),
        'fg': (234, 234, 236), 'dim': (156, 156, 162), 'sep': (108, 108, 114),
        'hot': [(60, (134, 201, 143)), (80, (92, 182, 224)), (999, (107, 116, 232))],
    },
    'light': {
        'pill': (250, 250, 252, 225), 'line': (120, 120, 128, 60),
        'fg': (27, 27, 30), 'dim': (91, 91, 96), 'sep': (150, 150, 156),
        'hot': [(60, (67, 158, 88)), (80, (23, 127, 196)), (999, (62, 62, 216))],
    },
}

# ---------------------------------------------------------------- Win32
user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

LRESULT = ctypes.c_longlong
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', w.UINT), ('style', w.UINT), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', w.HINSTANCE), ('hIcon', w.HICON), ('hCursor', w.HANDLE),
                ('hbrBackground', w.HBRUSH), ('lpszMenuName', w.LPCWSTR),
                ('lpszClassName', w.LPCWSTR), ('hIconSm', w.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [('hdc', w.HDC), ('fErase', w.BOOL), ('rcPaint', w.RECT),
                ('fRestore', w.BOOL), ('fIncUpdate', w.BOOL),
                ('rgbReserved', ctypes.c_byte * 32)]


class APPBARDATA(ctypes.Structure):
    _fields_ = [('cbSize', w.DWORD), ('hWnd', w.HWND), ('uCallbackMessage', w.UINT),
                ('uEdge', w.UINT), ('rc', w.RECT), ('lParam', w.LPARAM)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', w.DWORD), ('hWnd', w.HWND), ('uID', w.UINT),
                ('uFlags', w.UINT), ('uCallbackMessage', w.UINT), ('hIcon', w.HICON),
                ('szTip', w.WCHAR * 128), ('dwState', w.DWORD), ('dwStateMask', w.DWORD),
                ('szInfo', w.WCHAR * 256), ('uVersion', w.UINT),
                ('szInfoTitle', w.WCHAR * 64), ('dwInfoFlags', w.DWORD),
                ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', w.HICON)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [('dwLength', w.DWORD), ('dwMemoryLoad', w.DWORD),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [('ACLineStatus', ctypes.c_byte), ('BatteryFlag', ctypes.c_byte),
                ('BatteryLifePercent', ctypes.c_byte), ('SystemStatusFlag', ctypes.c_byte),
                ('BatteryLifeTime', w.DWORD), ('BatteryFullLifeTime', w.DWORD)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('fIcon', w.BOOL), ('xHotspot', w.DWORD), ('yHotspot', w.DWORD),
                ('hbmMask', w.HBITMAP), ('hbmColor', w.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', w.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                ('biPlanes', w.WORD), ('biBitCount', w.WORD), ('biCompression', w.DWORD),
                ('biSizeImage', w.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', w.DWORD),
                ('biClrImportant', w.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', w.DWORD * 3)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [('cbSize', w.DWORD), ('dwFlags', w.DWORD),
                ('hwndTrack', w.HWND), ('dwHoverTime', w.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


def _proto():
    P = ctypes.POINTER
    user32.RegisterClassExW.argtypes = [P(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = w.WORD
    user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.CreateWindowExW.restype = w.HWND
    user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.UpdateLayeredWindow.argtypes = [w.HWND, w.HDC, P(w.POINT), P(w.SIZE), w.HDC,
                                           P(w.POINT), w.DWORD, P(BLENDFUNCTION), w.DWORD]
    user32.UpdateLayeredWindow.restype = w.BOOL
    user32.GetDC.argtypes = [w.HWND]
    user32.GetDC.restype = w.HDC
    user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
    user32.GetWindowRect.argtypes = [w.HWND, P(w.RECT)]
    user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
    user32.CreatePopupMenu.restype = w.HMENU
    user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, w.HWND, P(w.RECT)]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, w.UINT]
    user32.DestroyIcon.argtypes = [w.HICON]
    user32.CreateIconIndirect.argtypes = [P(ICONINFO)]
    user32.CreateIconIndirect.restype = w.HICON
    user32.GetCursorPos.argtypes = [P(w.POINT)]
    user32.SetForegroundWindow.argtypes = [w.HWND]
    user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetCapture.argtypes = [w.HWND]
    user32.SetCapture.restype = w.HWND
    user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
    user32.GetMessageW.argtypes = [P(w.MSG), w.HWND, w.UINT, w.UINT]
    user32.DispatchMessageW.argtypes = [P(w.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.DrawTextW.argtypes = [w.HDC, w.LPCWSTR, ctypes.c_int, P(w.RECT), w.UINT]
    user32.FillRect.argtypes = [w.HDC, P(w.RECT), w.HBRUSH]
    user32.TrackMouseEvent.argtypes = [P(TRACKMOUSEEVENT)]

    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 13 + [w.LPCWSTR]
    gdi32.CreateFontW.restype = w.HFONT
    gdi32.CreateSolidBrush.argtypes = [w.DWORD]
    gdi32.CreateSolidBrush.restype = w.HBRUSH
    gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
    gdi32.SelectObject.restype = w.HGDIOBJ
    gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
    gdi32.SetBkMode.argtypes = [w.HDC, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [w.HDC, w.DWORD]
    gdi32.GetTextExtentPoint32W.argtypes = [w.HDC, w.LPCWSTR, ctypes.c_int, P(w.SIZE)]
    gdi32.CreateCompatibleDC.argtypes = [w.HDC]
    gdi32.CreateCompatibleDC.restype = w.HDC
    gdi32.DeleteDC.argtypes = [w.HDC]
    gdi32.CreateDIBSection.argtypes = [w.HDC, P(BITMAPINFO), w.UINT,
                                       P(ctypes.c_void_p), w.HANDLE, w.DWORD]
    gdi32.CreateDIBSection.restype = w.HBITMAP
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, w.LPVOID]
    gdi32.CreateBitmap.restype = w.HBITMAP
    gdi32.Ellipse.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.Pie.argtypes = [w.HDC] + [ctypes.c_int] * 8

    shell32.SHAppBarMessage.argtypes = [w.DWORD, P(APPBARDATA)]
    shell32.SHAppBarMessage.restype = ctypes.c_size_t
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, P(NOTIFYICONDATAW)]

    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel32.GetModuleHandleW.restype = w.HMODULE
    kernel32.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
    kernel32.CreateMutexW.restype = w.HANDLE
    kernel32.GlobalMemoryStatusEx.argtypes = [P(MEMORYSTATUSEX)]
    kernel32.GetSystemPowerStatus.argtypes = [P(SYSTEM_POWER_STATUS)]


_proto()


# ---------------------------------------------------------------- 配置
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_PATH, encoding='utf-8') as fh:
            cfg.update(json.load(fh) or {})
    except Exception:
        pass
    valid = dict(FIELDS)
    cfg['fields'] = [f for f in (cfg.get('fields') or []) if f in valid] or ['freq', 'temp', 'fan']
    try:
        cfg['scale'] = min(1.5, max(0.7, float(cfg.get('scale') or 1.0)))
    except Exception:
        cfg['scale'] = 1.0
    return cfg


def save_cfg(cfg):
    try:
        tmp = CFG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception:
        pass


def system_is_light():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            v, _ = winreg.QueryValueEx(k, 'SystemUsesLightTheme')
            return bool(v)
    except Exception:
        return False


# ---------------------------------------------------------------- 数据
def _json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def bgr(rgb):
    """(R,G,B) -> COLORREF 0x00BBGGRR"""
    return rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)


def _fresh(d, secs=15.0):
    return bool(d) and (time.time() - float(d.get('ts') or 0)) < secs


def collect(cfg):
    """返回 [(文字, 角色)]，角色: fg / dim / sep / hot0..hot2"""
    cpu = _json(os.path.join(STATUS_DIR, 'cpu.json'))
    fan = _json(os.path.join(STATUS_DIR, 'fan.json'))
    cpu = cpu if _fresh(cpu) else {}
    fan = fan if _fresh(fan) else {}
    segs = []

    for f in cfg['fields']:
        if f == 'freq':
            v = cpu.get('freq_mhz')
            segs.append(('%.1fG' % (v / 1000.0) if v else '--G', 'fg' if v else 'dim'))
        elif f in ('temp', 'dts'):
            # EC 温度优先用风扇服务(5 秒一读)；CPU 服务的 EC 缓存是 15 秒一读, 会滞后
            t = fan.get('ec') if f == 'temp' else cpu.get('dts')
            if t is None:
                t = cpu.get('ec') if f == 'temp' else fan.get('dts')
            if t is None:
                segs.append(('--\u00b0', 'dim'))
            else:
                segs.append(('%d\u00b0' % t, 'hot0' if t < 60 else ('hot1' if t < 80 else 'hot2')))
        elif f == 'fan':
            v = fan.get('rpm')
            segs.append(('%d' % v if v else '----', 'fg' if v else 'dim'))
        elif f == 'gear':
            v = fan.get('hfsp')
            if v is None:
                segs.append(('--', 'dim'))
            elif v & 0x80:
                segs.append(('\u81ea\u52a8', 'dim'))
            else:
                segs.append(('%d\u6863' % (v & 0x7F), 'fg'))
        elif f == 'pkg':
            v = fan.get('pkg')
            segs.append(('%dW' % round(v) if v else '--W', 'fg' if v else 'dim'))
        elif f == 'pl':
            if cpu.get('pl1'):
                segs.append(('%d/%d' % (round(cpu['pl1']), round(cpu['pl2'])), 'dim'))
            else:
                segs.append(('--/--', 'dim'))
        elif f == 'mem':
            ms = MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(ms)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                segs.append(('%d%%' % ms.dwMemoryLoad, 'dim'))
            else:
                segs.append(('--%', 'dim'))
        elif f == 'batt':
            sp = SYSTEM_POWER_STATUS()
            if kernel32.GetSystemPowerStatus(ctypes.byref(sp)) and sp.BatteryLifePercent != 255:
                segs.append(('%d%%' % sp.BatteryLifePercent, 'dim'))
            else:
                segs.append(('--%', 'dim'))
        elif f == 'guard':
            lv = cpu.get('level')
            name = {'warn': '\u964d\u6863', 'crit': '\u538b1', 'crit2': '\u538b2',
                    'crit3': '\u538b3', 'flag': '\u6807\u5fd7', 'trip': '\u6807\u5fd7'}.get(lv or '', '\u6b63\u5e38')
            segs.append((name, 'hot1' if lv else 'dim'))
        elif f == 'time':
            segs.append((time.strftime('%H:%M'), 'dim'))
    return segs


# ---------------------------------------------------------------- 窗口
class Hud(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.hwnd = None
        self.font = None
        self.font_px = 0
        self.segs = []
        self.widths = []
        self.spans = []          # [(x0, x1, role)]
        self.dragging = False
        self.drag_off = (0, 0)
        self.nid = None
        self.icon = None
        self.icon_col = None
        self.hover = False
        self._alert = {}          # {'cpu': 上次提醒时间, 'fan': ...}
        self.visible = bool(cfg.get('enabled', True))
        self.auto_pos = cfg.get('x') is None
        self.wndproc_ref = WNDPROC(self._wndproc)
        self.hinst = kernel32.GetModuleHandleW(None)
        self.buf_size = (0, 0)
        self.memdc = None
        self.dib = None
        self.bits = None
        self.tmemdc = None
        self.tdib = None
        self.tbits = None

    # ---- 尺寸
    def taskbar(self):
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(abd)
        if shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd)):
            return abd.rc, max(20, abd.rc.bottom - abd.rc.top)
        return w.RECT(0, 0, 1920, 1080), 40

    def metrics(self):
        rc, tb_h = self.taskbar()
        s = float(self.cfg.get('scale') or 1.0)
        fh = max(9, int(round(tb_h * 0.26 * s)))          # 与任务栏时钟字高相当
        hei = min(tb_h - 4, max(fh + 8, int(round(tb_h * 0.62 * s))))
        pad = max(4, int(round(fh * 0.70)))
        gap = max(6, int(round(fh * 0.62)))
        radius = max(3, int(round(hei * 0.34)))
        return rc, tb_h, fh, hei, pad, gap, radius

    def palette(self):
        theme = self.cfg.get('theme') or 'auto'
        if theme == 'auto':
            theme = 'light' if system_is_light() else 'dark'
        return PALETTE.get(theme, PALETTE['dark'])

    def measure(self, hdc, text):
        sz = w.SIZE()
        gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
        return sz.cx

    def make_font(self, hdc, fh):
        if self.font and self.font_px == fh:
            return
        if self.font:
            gdi32.DeleteObject(self.font)
        self.font_px = fh
        self.font = gdi32.CreateFontW(-fh, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 4, 0,
                                      'Segoe UI Variable Text')

    # ---- 双缓冲 DIB
    def ensure_buffers(self, wid, hei):
        if self.buf_size == (wid, hei) and self.memdc:
            return
        self.free_buffers()
        hdc = user32.GetDC(None)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = wid
        bmi.bmiHeader.biHeight = -hei          # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32

        def mk():
            dc = gdi32.CreateCompatibleDC(hdc)
            bits = ctypes.c_void_p()
            dib = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            gdi32.SelectObject(dc, dib)
            return dc, dib, bits

        self.memdc, self.dib, self.bits = mk()
        self.tmemdc, self.tdib, self.tbits = mk()
        user32.ReleaseDC(None, hdc)
        self.buf_size = (wid, hei)

    def free_buffers(self):
        for dc, dib in ((self.memdc, self.dib), (self.tmemdc, self.tdib)):
            if dib:
                gdi32.DeleteObject(dib)
            if dc:
                gdi32.DeleteDC(dc)
        self.memdc = self.dib = self.bits = None
        self.tmemdc = self.tdib = self.tbits = None
        self.buf_size = (0, 0)

    # ---- 建窗
    def create(self):
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = self.wndproc_ref
        wc.hInstance = self.hinst
        wc.lpszClassName = APP_NAME
        user32.RegisterClassExW(ctypes.byref(wc))

        rc, tb_h, fh, hei, pad, gap, radius = self.metrics()
        x = rc.right - int(self.cfg.get('offset_right') or 470) - 170
        y = rc.top + (tb_h - hei) // 2
        if self.cfg.get('y') is not None:
            y = int(self.cfg['y'])
        if self.cfg.get('x') is not None:
            x = int(self.cfg['x'])

        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
            APP_NAME, APP_NAME, WS_POPUP, x, y, 170, hei, None, None, self.hinst, None)
        if not self.hwnd:
            raise OSError('CreateWindowExW 失败: %d' % ctypes.get_last_error())
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if self.visible else SW_HIDE)
        user32.SetTimer(self.hwnd, 1, 1000, None)
        self.apply_lock()
        self.tray_add()
        self.refresh()

    # ---- 合成 + 上屏
    def refresh(self):
        if not self.hwnd:
            return
        self.segs = collect(self.cfg)
        rc, tb_h, fh, hei, pad, gap, radius = self.metrics()
        hdc = user32.GetDC(self.hwnd)
        try:
            self.make_font(hdc, fh)
            gdi32.SelectObject(hdc, self.font)
            self.widths = [self.measure(hdc, t) for t, _r in self.segs]
            wid = max(40, sum(self.widths) + gap * max(0, len(self.segs) - 1) + pad * 2)
        finally:
            user32.ReleaseDC(self.hwnd, hdc)

        r = w.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        x, y = r.left, r.top
        if self.auto_pos:
            x = rc.right - int(self.cfg.get('offset_right') or 470) - wid
            x = max(rc.left + 6, min(x, rc.right - wid - 6))
            y = rc.top + (tb_h - hei) // 2
        if (r.right - r.left) != wid or (r.bottom - r.top) != hei:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, wid, hei, SWP_NOACTIVATE)
        elif self.auto_pos and (r.left != x or r.top != y):
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)
        self.render(wid, hei, pad, gap, radius)

    def render(self, wid, hei, pad, gap, radius):
        self.ensure_buffers(wid, hei)
        pal = self.palette()

        # 1) 文字画到临时 DIB（白字黑底 -> 亮度即覆盖度）
        ctypes.memset(self.tbits, 0, wid * hei * 4)
        gdi32.SetBkMode(self.tmemdc, TRANSPARENT)
        gdi32.SelectObject(self.tmemdc, self.font)
        gdi32.SetTextColor(self.tmemdc, 0x00FFFFFF)
        box = w.RECT(0, 0, 0, hei)
        x = pad
        self.spans = []
        for i, (txt, role) in enumerate(self.segs):
            if i:
                x += gap
            bw = self.widths[i] if i < len(self.widths) else 0
            box.left, box.right = x, x + bw + 3
            user32.DrawTextW(self.tmemdc, txt, -1, ctypes.byref(box),
                             DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
            self.spans.append((x, x + bw + 3, role))
            x += bw

        cov = ctypes.string_at(self.tbits, wid * hei * 4)

        # 2) 背景 + 文字合成（预乘 alpha）
        pill = self.cfg.get('pill', True)
        pr, pg, pb, pa = pal['pill']
        if self.hover and pill:
            pr, pg, pb = min(255, pr + 14), min(255, pg + 14), min(255, pb + 14)
        lr, lg, lb, la = pal['line']
        r = radius

        def inside(px, py):
            """圆角矩形内。"""
            cx = min(max(px, r), wid - 1 - r)
            cy = min(max(py, r), hei - 1 - r)
            dx = px - cx
            dy = py - cy
            return dx * dx + dy * dy <= r * r

        colors = {k: pal[k] for k in ('fg', 'dim', 'sep')}
        hot = dict(pal['hot'])
        out = bytearray(wid * hei * 4)

        for py in range(hei):
            row = py * wid
            for px in range(wid):
                idx = (row + px) * 4
                ai = (row + px) * 4
                a_text = cov[ai]          # 灰度即覆盖度（白字）
                a_bg = 0
                br = bg_ = bb = 0
                if pill and inside(px, py):
                    a_bg = pa
                    br, bg_, bb = pr, pg, pb
                    # 1px 浅色描边（外侧一圈）
                    if not inside(px - 1, py) or not inside(px + 1, py) \
                            or not inside(px, py - 1) or not inside(px, py + 1):
                        a_bg = min(255, pa + la)
                        br = (pr * pa + lr * la) // max(1, pa + la)
                        bg_ = (pg * pa + lg * la) // max(1, pa + la)
                        bb = (pb * pa + lb * la) // max(1, pa + la)
                if a_text:
                    # 该像素属于哪个文字段（取颜色）
                    col = pal['fg']
                    for (x0, x1, role) in self.spans:
                        if x0 <= px < x1:
                            if role.startswith('hot'):
                                lim = int(role[3])
                                col = hot[pal['hot'][lim][0]]
                            else:
                                col = colors.get(role, pal['fg'])
                            break
                    tr, tg, tb = col
                    na = a_text + a_bg * (255 - a_text) // 255
                    nr = (tr * a_text + br * a_bg * (255 - a_text) // 255) // max(1, na)
                    ng = (tg * a_text + bg_ * a_bg * (255 - a_text) // 255) // max(1, na)
                    nb = (tb * a_text + bb * a_bg * (255 - a_text) // 255) // max(1, na)
                    br, bg_, bb, a_bg = nr, ng, nb, na
                if a_bg:
                    out[idx] = bb * a_bg // 255
                    out[idx + 1] = bg_ * a_bg // 255
                    out[idx + 2] = br * a_bg // 255
                    out[idx + 3] = a_bg

        ctypes.memmove(self.bits, bytes(out), len(out))
        screen = user32.GetDC(None)
        try:
            wr = w.RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(wr))
            pt_dst = w.POINT(wr.left, wr.top)
            sz = w.SIZE(wid, hei)
            pt_src = w.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pt_dst),
                                       ctypes.byref(sz), self.memdc, ctypes.byref(pt_src),
                                       0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)

    # ---- 托盘
    def tray_add(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        self.icon_col = self.icon_color()
        self.icon = self.make_fan_icon(self.icon_col)
        nid.hIcon = self.icon
        nid.szTip = APP_NAME
        self.nid = nid
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def icon_color(self):
        cpu = _json(os.path.join(STATUS_DIR, 'cpu.json'))
        t = (cpu.get('ec') or cpu.get('dts')) if _fresh(cpu) else None
        pal = self.palette()
        if t is None:
            return bgr(pal['dim'])
        for lim, c in pal['hot']:
            if t < lim:
                return bgr(c)
        return bgr(pal['hot'][-1][1])

    def make_fan_icon(self, color_rgb):
        """三叶风扇：彩色圆盘 + 3 个等分深色扇形 + 白轴心（64 源图，系统缩放）。"""
        try:
            size = 64
            c = size // 2
            hdc = user32.GetDC(None)
            mem = gdi32.CreateCompatibleDC(hdc)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = size
            bmi.bmiHeader.biHeight = -size
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bits = ctypes.c_void_p()
            color = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            mask = gdi32.CreateBitmap(size, size, 1, 1, None)
            old = gdi32.SelectObject(mem, color)

            white = gdi32.CreateSolidBrush(0x00FFFFFF)
            black = gdi32.CreateSolidBrush(0x00000000)
            disc = gdi32.CreateSolidBrush(color_rgb)
            blade = gdi32.CreateSolidBrush(0x001A1A1C)
            hub = gdi32.CreateSolidBrush(0x00FFFFFF)
            rect = w.RECT(0, 0, size, size)

            user32.FillRect(mem, ctypes.byref(rect), black)

            gdi32.SelectObject(mem, disc)      # 扇叶用温度色，底透明
            for i in range(3):
                # Pie 在屏幕坐标里按逆时针取弧: 想画 a1->a2 的 84 度，必须把 a2 当起点传
                a1 = math.radians(i * 120 + 24)
                a2 = math.radians(i * 120 + 86)
                gdi32.Pie(mem, 0, 0, size, size,
                          int(round(c + c * math.cos(a2))), int(round(c + c * math.sin(a2))),
                          int(round(c + c * math.cos(a1))), int(round(c + c * math.sin(a1))))

            gdi32.SelectObject(mem, hub)
            gdi32.Ellipse(mem, c - 6, c - 6, c + 6, c + 6)

            # GDI 画完 alpha 全 0：32bpp 图标的 alpha 会被尊重，必须自己写。
            # 只给圆内像素 alpha=255，圆外=0（否则 mask 被忽略，整个方块都是黑的）
            buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
            # 透明底：alpha 直接取亮度（黑底上画的彩色/白色图形，边缘自带抗锯齿）
            for i in range(0, size * size * 4, 4):
                buf[i + 3] = max(buf[i], buf[i + 1], buf[i + 2])

            mmem = gdi32.CreateCompatibleDC(hdc)
            gdi32.SelectObject(mmem, mask)
            user32.FillRect(mmem, ctypes.byref(rect), white)
            gdi32.SelectObject(mmem, black)
            gdi32.Ellipse(mmem, 0, 0, size, size)
            gdi32.DeleteDC(mmem)

            for b in (white, black, disc, blade, hub):
                gdi32.DeleteObject(b)
            gdi32.SelectObject(mem, old)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, hdc)

            ii = ICONINFO(True, 0, 0, mask, color)
            ic = user32.CreateIconIndirect(ctypes.byref(ii))
            gdi32.DeleteObject(color)
            gdi32.DeleteObject(mask)
            return ic
        except Exception as exc:
            print('icon error:', exc)
            return None

    def tray_tip(self):
        if not self.nid:
            return
        col = self.icon_color()
        if col != self.icon_col:
            self.icon_col = col
            new = self.make_fan_icon(col)
            if new:
                old = self.icon
                self.icon = new
                self.nid.hIcon = new
                if old:
                    user32.DestroyIcon(old)
        txt = ' '.join(t for t, _r in self.segs)
        self.nid.szTip = ('%s  %s' % (APP_NAME, txt))[:120]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def apply_lock(self):
        """锁定位置 = 加 WS_EX_TRANSPARENT，鼠标点击穿过（就不会挡住任务栏图标）。"""
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        if self.cfg.get('locked'):
            ex |= WS_EX_TRANSPARENT
        else:
            ex &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex)

    def notify(self, title, text):
        """托盘气泡提醒。"""
        if not self.nid:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_WARNING
        nid.uVersion = 10000          # 这个位置在 NIF_INFO 下是超时(毫秒)
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def check_alerts(self):
        """服务掉线超过 60 秒提醒一次（同一来源 10 分钟内不重复）。"""
        now = time.time()
        for key, fname, label in (('cpu', 'cpu.json', 'CPU 服务'), ('fan', 'fan.json', '风扇服务')):
            d = _json(os.path.join(STATUS_DIR, fname))
            age = now - float((d or {}).get('ts') or 0)
            if age > 60:
                last = self._alert.get(key, 0)
                if now - last > 600:
                    self._alert[key] = now
                    self.notify('T14scontrol', '%s 已 %d 秒没有更新，功耗墙/风扇可能没在接管。'
                                % (label, int(age)))

    def set_visible(self, on):
        self.visible = bool(on)
        self.cfg['enabled'] = self.visible
        save_cfg(self.cfg)
        if self.visible:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self.refresh()
        else:
            user32.ShowWindow(self.hwnd, SW_HIDE)

    # ---- 菜单
    def menu(self):
        hm = user32.CreatePopupMenu()
        user32.AppendMenuW(hm, MF_STRING, 100, '隐藏' if self.visible else '显示')
        user32.AppendMenuW(hm, MF_SEPARATOR, 0, None)
        cur = set(self.cfg['fields'])
        for i, (key, label) in enumerate(FIELDS):
            user32.AppendMenuW(hm, MF_STRING | (MF_CHECKED if key in cur else 0), 200 + i, label)
        user32.AppendMenuW(hm, MF_STRING, 250, '全部显示')
        user32.AppendMenuW(hm, MF_STRING, 251, '只留 频率/温度/风扇')
        user32.AppendMenuW(hm, MF_SEPARATOR, 0, None)
        sc = float(self.cfg.get('scale') or 1.0)
        for i, (label, val) in enumerate(SCALES):
            user32.AppendMenuW(hm, MF_STRING | (MF_CHECKED if abs(sc - val) < 0.02 else 0),
                               500 + i, '尺寸: ' + label)
        theme = self.cfg.get('theme') or 'auto'
        for i, (label, val) in enumerate((('跟随系统', 'auto'), ('深色', 'dark'), ('浅色', 'light'))):
            user32.AppendMenuW(hm, MF_STRING | (MF_CHECKED if theme == val else 0),
                               510 + i, '外观: ' + label)
        user32.AppendMenuW(hm, MF_STRING | (MF_CHECKED if self.cfg.get('pill', True) else 0),
                           520, '底框')
        user32.AppendMenuW(hm, MF_STRING | (MF_CHECKED if self.cfg.get('locked') else 0),
                           521, '锁定位置（点击穿过）')
        user32.AppendMenuW(hm, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hm, MF_STRING | (MF_CHECKED if is_autostart() else 0), 300, '开机自启')
        user32.AppendMenuW(hm, MF_STRING, 301, '恢复默认位置')
        user32.AppendMenuW(hm, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hm, MF_STRING, 400, '退出')

        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(hm, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(hm)
        self.on_menu(cmd)

    def on_menu(self, cmd):
        if cmd == 100:
            self.set_visible(not self.visible)
        elif 200 <= cmd < 200 + len(FIELDS):
            key = FIELDS[cmd - 200][0]
            f = list(self.cfg['fields'])
            if key in f:
                if len(f) > 1:
                    f.remove(key)
            else:
                f.append(key)
            self.cfg['fields'] = [k for k, _l in FIELDS if k in f]
            save_cfg(self.cfg)
            self.refresh()
        elif cmd == 250:
            self.cfg['fields'] = [k for k, _l in FIELDS]
            save_cfg(self.cfg)
            self.refresh()
        elif cmd == 251:
            self.cfg['fields'] = ['freq', 'temp', 'fan']
            save_cfg(self.cfg)
            self.refresh()
        elif 500 <= cmd < 500 + len(SCALES):
            self.cfg['scale'] = SCALES[cmd - 500][1]
            save_cfg(self.cfg)
            self.refresh()
        elif 510 <= cmd < 513:
            self.cfg['theme'] = ('auto', 'dark', 'light')[cmd - 510]
            save_cfg(self.cfg)
            self.icon_col = None
            self.refresh()
            self.tray_tip()
        elif cmd == 520:
            self.cfg['pill'] = not self.cfg.get('pill', True)
            save_cfg(self.cfg)
            self.refresh()
        elif cmd == 521:
            self.cfg['locked'] = not self.cfg.get('locked')
            save_cfg(self.cfg)
            self.apply_lock()
        elif cmd == 300:
            set_autostart(not is_autostart())
        elif cmd == 301:
            self.cfg['x'] = self.cfg['y'] = None
            self.auto_pos = True
            save_cfg(self.cfg)
            self.refresh()
        elif cmd == 400:
            user32.DestroyWindow(self.hwnd)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_PAINT:
                return 0                     # 全部走 UpdateLayeredWindow
            if msg == WM_TIMER:
                if self.visible:
                    self.refresh()
                self.tray_tip()
                self.check_alerts()
                return 0
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF
                if ev in (WM_RBUTTONUP, 0x0206):
                    self.menu()
                elif ev == WM_LBUTTONDBLCLK:
                    self.set_visible(not self.visible)
                return 0
            if msg == WM_LBUTTONDOWN:
                self.dragging = True
                pt = w.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                r = w.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                self.drag_off = (pt.x - r.left, pt.y - r.top)
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE:
                if self.dragging:
                    pt = w.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, pt.x - self.drag_off[0],
                                        pt.y - self.drag_off[1], 0, 0,
                                        SWP_NOSIZE | SWP_NOACTIVATE)
                elif not self.hover:
                    self.hover = True
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x00000002, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    self.refresh()
                return 0
            if msg == WM_MOUSELEAVE:
                if self.hover:
                    self.hover = False
                    self.refresh()
                return 0
            if msg == WM_LBUTTONUP and self.dragging:
                self.dragging = False
                user32.ReleaseCapture()
                r = w.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                self.cfg['x'], self.cfg['y'] = r.left, r.top
                self.auto_pos = False
                save_cfg(self.cfg)
                return 0
            if msg == WM_RBUTTONUP:
                self.menu()
                return 0
            if msg == WM_DESTROY:
                if self.nid:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
                user32.KillTimer(hwnd, 1)
                self.free_buffers()
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:
            print('wndproc error:', exc)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        self.create()
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------- 自启
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'T14scontrolHUD'


def pythonw():
    exe = sys.executable
    if exe.lower().endswith('python.exe'):
        cand = exe[:-len('python.exe')] + 'pythonw.exe'
        if os.path.exists(cand):
            return cand
    return exe


def is_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_NAME)
            return bool(v)
    except Exception:
        return False


def set_autostart(on):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ,
                                  '"%s" "%s"' % (pythonw(), os.path.join(BASE, 'hud.py')))
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
    except Exception as exc:
        print('autostart error:', exc)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    kernel32.CreateMutexW(None, False, APP_NAME + '_mutex')
    if ctypes.get_last_error() == 183:
        return 0

    cfg = load_cfg()
    if '--reset' in sys.argv:
        cfg['x'] = cfg['y'] = None
        save_cfg(cfg)
    Hud(cfg).run()
    return 0


if __name__ == '__main__':
    sys.exit(main())

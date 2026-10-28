# -*- coding: utf-8 -*-
"""
uart_port.py —— 串口端口的统一入口（pyserial 优先，缺则用 ctypes 直连 Win32）

为什么要有这个文件
------------------
链路自检脚本（fpga_uart_echo_check / fpga_uart_tone）原来直接 `import serial`。
本机（以及任何刚装完系统的机器）没有 pyserial，而这里又是离线环境装不上，
结果就是"想验证硬件却连脚本都跑不起来"。

串口是硬件调试的第一步，不能因为一个纯 Python 包缺失就断掉。所以这里做
一层薄封装：有 pyserial 就用它，没有就直接用 ctypes 调 CreateFileW /
BuildCommDCB / WriteFile / ReadFile，功能覆盖本项目需要的部分。

对上层只暴露和 pyserial 同名的一小撮方法：
    open / close / write / read / reset_input_buffer / reset_output_buffer
"""

import os
import sys

try:
    import serial as _serial          # pyserial
except Exception:
    _serial = None


class Win32Serial(object):
    """Windows 串口的最小实现（ctypes 直连 kernel32）。

    只覆盖本项目用到的 8N1 全双工读写，不做调制解调器控制线。
    """

    def __init__(self, port, baudrate, timeout=0.05, write_timeout=1.0):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self._h = None
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateFileW.restype = wintypes.HANDLE
        self._k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._k32.WriteFile.argtypes = [
            wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self._k32.WriteFile.restype = wintypes.BOOL
        self._k32.ReadFile.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self._k32.ReadFile.restype = wintypes.BOOL
        self._open()

    # ------------------------------------------------------------ 打开
    def _open(self):
        ctypes = self.ctypes
        from ctypes import wintypes

        # \\.\ 前缀是访问 COM10 以上（以及避免设备名冲突）的标准写法
        name = self.port if self.port.startswith("\\\\") else "\\\\.\\" + self.port
        h = self._k32.CreateFileW(
            name,
            0x80000000 | 0x40000000,        # GENERIC_READ | GENERIC_WRITE
            0,                              # 独占
            None, 3,                        # OPEN_EXISTING
            0, None)
        INVALID = ctypes.c_void_p(-1).value
        if h is None or h == INVALID or (isinstance(h, int) and h == INVALID):
            err = ctypes.get_last_error()
            raise IOError("CreateFileW(%s) 失败，GetLastError=%d"
                          % (self.port, err))

        # ---- DCB：用 BuildCommDCB 按 "baud=... parity=N data=8 stop=1" 填 ----
        dcb = ctypes.create_string_buffer(64)
        ctypes.memset(dcb, 0, 64)
        # DCB 的第一个 DWORD 是 DCBlength，BuildCommDCB 要求先填好（28）
        ctypes.cast(dcb, ctypes.POINTER(ctypes.c_uint32))[0] = 28
        cfg = "baud=%d parity=N data=8 stop=1" % self.baudrate
        if not self._k32.BuildCommDCBW(cfg, dcb):
            self._k32.CloseHandle(h)
            raise IOError("BuildCommDCBW 失败：%s（该波特率驱动可能不支持）" % cfg)
        if not self._k32.SetCommState(h, dcb):
            self._k32.CloseHandle(h)
            raise IOError("SetCommState 失败，GetLastError=%d"
                          % ctypes.get_last_error())

        # ---- 超时：读用总超时常量，写按 write_timeout ----
        to = ctypes.create_string_buffer(20)
        vals = [0xFFFFFFFF, 0, max(1, int(self.timeout * 1000.0)),
                0, max(1, int(self.timeout * 1000.0))]
        for i, v in enumerate(vals):
            ctypes.cast(to, ctypes.POINTER(ctypes.c_uint32))[i] = v
        self._k32.SetCommTimeouts(h, to)
        self._h = h
        self._buf = ctypes.create_string_buffer(65536)
        self._nread = ctypes.c_uint32(0)

    # ------------------------------------------------------------ IO
    @property
    def is_open(self):
        return self._h is not None

    def write(self, data):
        ctypes = self.ctypes
        from ctypes import wintypes

        if self._h is None:
            raise IOError("端口未打开")
        if isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
        else:
            raw = bytes(bytearray(data))
        if not raw:
            return 0
        c = ctypes.c_char * len(raw)
        n = wintypes.DWORD(0)
        ok = self._k32.WriteFile(self._h, c.from_buffer_copy(raw),
                                 len(raw), ctypes.byref(n), None)
        if not ok:
            raise IOError("WriteFile 失败，GetLastError=%d"
                          % ctypes.get_last_error())
        return n.value

    def read(self, size=65536):
        ctypes = self.ctypes
        from ctypes import wintypes

        if self._h is None:
            raise IOError("端口未打开")
        size = min(int(size), 65536)
        n = wintypes.DWORD(0)
        ok = self._k32.ReadFile(self._h, self._buf, size,
                                ctypes.byref(n), None)
        # 超时也算"成功"，只是读到 0 字节（ERROR_SUCCESS 或 ERROR_TIMEOUT）
        if not ok and ctypes.get_last_error() not in (0, 995, 121):
            raise IOError("ReadFile 失败，GetLastError=%d"
                          % ctypes.get_last_error())
        return self._buf.raw[:n.value]

    def reset_input_buffer(self):
        if self._h is not None:
            self._k32.PurgeComm(self._h, 0x0008 | 0x0001)   # RXCLEAR | RXABORT
            # 再读一次把驱动缓冲里残留的字节吸干
            try:
                while self.read(4096):
                    pass
            except Exception:
                pass

    def reset_output_buffer(self):
        if self._h is not None:
            self._k32.PurgeComm(self._h, 0x0004 | 0x0002)   # TXCLEAR | TXABORT

    @property
    def out_waiting(self):
        return 0

    def close(self):
        if self._h is not None:
            try:
                self._k32.CloseHandle(self._h)
            except Exception:
                pass
            self._h = None


def open_serial(port, baudrate, timeout=0.05, write_timeout=1.0):
    """打开串口。返回 (对象, 后端名)。"""
    if _serial is not None:
        s = _serial.Serial(port, baudrate, bytesize=8, parity="N", stopbits=1,
                           timeout=timeout, write_timeout=write_timeout)
        return s, "pyserial"
    if sys.platform.startswith("win"):
        return Win32Serial(port, baudrate, timeout=timeout,
                           write_timeout=write_timeout), "ctypes/Win32"
    raise RuntimeError("既没有 pyserial，也不是 Windows：无法打开串口")


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "COM4"
    try:
        s, backend = open_serial(p, 1500000, timeout=0.2)
    except Exception as e:
        print("OPEN FAIL %s: %s" % (p, e))
        sys.exit(2)
    print("OPEN OK %s via %s" % (p, backend))
    s.close()

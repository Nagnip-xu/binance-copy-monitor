"""A thread-scoped Windows sleep request; never changes the power plan."""
from __future__ import annotations

import ctypes
import os


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self, enabled: bool, *, setter=None):
        self.enabled = enabled
        self.setter = setter
        self.active = False

    def __enter__(self):
        if self.enabled:
            try:
                if self.setter is None and os.name == "nt":
                    self.setter = ctypes.windll.kernel32.SetThreadExecutionState
                    self.setter.argtypes = [ctypes.c_uint]
                    self.setter.restype = ctypes.c_uint
                if self.setter is not None:
                    self.active = bool(self.setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
            except (AttributeError, OSError):
                self.active = False
        return self

    def __exit__(self, *_):
        if self.active:
            try:
                self.setter(ES_CONTINUOUS)
            except OSError:
                pass  # Windows also releases this thread's request when it exits.
            finally:
                self.active = False

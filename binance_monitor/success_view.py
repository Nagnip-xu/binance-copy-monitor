"""Nonblocking receipts for newly confirmed joins, shown by the Tk main thread."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .core import Event


def success_text(event: Event, *, demo: bool = False) -> str:
    receipt = event.confirmation
    if event.kind != "joined" or receipt is None or event.snapshot is None:
        raise ValueError("Only a newly confirmed join has a success receipt.")
    model = {"FIXED_RATIO": "等比跟单", "FIXED_AMT": "定额跟单"}.get(receipt.copy_model, "历史模式未记录")
    return (f"{'【演示数据】' if demo else ''}已成功加入 {event.snapshot.nickname}\n"
            f"Portfolio ID：{event.target}\n"
            f"模式：{model}\n"
            f"投入保证金：{format(receipt.amount, 'f')} USDT\n"
            f"跟单组合 ID：{receipt.copy_id}")


class SuccessNotifications:
    def __init__(self, parent: tk.Tk):
        self.parent = parent
        self.seen: set[tuple[bool, str]] = set()
        self.windows: set[tk.Toplevel] = set()

    def show(self, event: Event, *, demo: bool = False) -> tk.Toplevel | None:
        if event.kind != "joined" or event.confirmation is None or event.snapshot is None:
            return None
        key = (demo, event.confirmation.operation_id)
        if key in self.seen:
            return None
        message = success_text(event, demo=demo)
        window = tk.Toplevel(self.parent)
        window.withdraw()
        window.title("币安跟单加入成功" + ("（演示）" if demo else ""))
        window.configure(background="#FFFFFF")
        window.resizable(False, False)
        if self.parent.state() == "normal":
            window.transient(self.parent)

        body = tk.Frame(window, background="#FFFFFF", padx=24, pady=24)
        body.pack(fill="both", expand=True)
        tk.Label(body, bitmap="info", background="#FFFFFF", foreground="#0078D4").pack(
            side="left", anchor="n", padx=(0, 16))
        tk.Label(body, text=message, background="#FFFFFF", foreground="#18212B",
                 font=("Microsoft YaHei UI", 10), justify="left", anchor="w",
                 wraplength=min(520, int(window.winfo_screenwidth() * .7))).pack(side="left", fill="both", expand=True)
        footer = tk.Frame(window, background="#F3F5F7", padx=20, pady=14)
        footer.pack(fill="x")

        def dismiss(_event=None):
            window.destroy()

        button = ttk.Button(footer, text="确定", command=dismiss, width=12)
        button.pack(side="right")
        window.bind("<Return>", dismiss)
        window.bind("<Escape>", dismiss)
        window.protocol("WM_DELETE_WINDOW", dismiss)
        topmost_job = None

        def cleanup(event):
            if event.widget is window:
                self.windows.discard(window)
                if topmost_job is not None:
                    window.after_cancel(topmost_job)

        window.bind("<Destroy>", cleanup)
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x = self.parent.winfo_x() + (self.parent.winfo_width() - width) // 2
        y = self.parent.winfo_y() + (self.parent.winfo_height() - height) // 2
        x = max(0, min(x, window.winfo_screenwidth() - width))
        y = max(0, min(y, window.winfo_screenheight() - height - 48))
        window.geometry(f"+{x}+{y}")
        window.deiconify()
        window.lift()
        window.attributes("-topmost", True)
        # Present it above other apps without permanently covering their windows.
        topmost_job = window.after(1500, lambda: window.attributes("-topmost", False))
        button.focus_set()
        self.seen.add(key)
        self.windows.add(window)
        # No wait_window or grab: unattended monitoring must keep processing events.
        return window

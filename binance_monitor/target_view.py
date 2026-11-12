"""Compact, read-only target table; rendering never makes account requests."""
from __future__ import annotations

import tkinter.font as tkfont
from tkinter import ttk

from .core import Snapshot


class TargetTable(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.targets = ()
        self.snapshots: dict[str, Snapshot] = {}
        self.startup: dict[str, bool] = {}
        self.balance = None
        self.copy_model = "FIXED_RATIO"
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text="监控对象").grid(row=0, column=0, sticky="w", pady=(0, 6))
        columns = ("portfolio", "nickname", "capacity", "minimum", "balance")
        font = tkfont.nametofont("TkDefaultFont")
        style = ttk.Style(self)
        style.configure("Targets.Treeview", font=font, rowheight=font.metrics("linespace") + 8,
                        background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#202020")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=2,
                                 style="Targets.Treeview", selectmode="browse")
        for key, title, sample in (
                ("portfolio", "Portfolio ID", "5075281354358777856"),
                ("nickname", "带单员", "带单员名称名称"),
                ("capacity", "名额", "1000/1000（启动）"),
                ("minimum", "最低金额", "300.1234 USDT"),
                ("balance", "现货余额", "302.90619161 USDT")):
            width = font.measure(sample) + 30
            self.tree.heading(key, text=title, anchor="center")
            self.tree.column(key, width=width, minwidth=width, anchor="center")
        self.tree.grid(row=1, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        ttk.Label(self, text="名额、余额为最近查询结果；直接抢位期间不额外刷新。",
                  style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(3, 0))

    def set_targets(self, targets, *, reset=False):
        targets = tuple(targets)
        if reset:
            self.snapshots.clear()
            self.startup.clear()
            self.balance = None
        self.snapshots = {target: snapshot for target, snapshot in self.snapshots.items() if target in targets}
        self.startup = {target: startup for target, startup in self.startup.items() if target in targets}
        self.targets = targets
        for item in self.tree.get_children():
            if item not in targets:
                self.tree.delete(item)
        for index, target in enumerate(targets):
            if not self.tree.exists(target):
                self.tree.insert("", "end", iid=target)
            self.tree.move(target, "", index)
            self._render(target)
        self.tree.configure(height=max(2, min(3, len(targets))))

    def update_snapshot(self, snapshot: Snapshot, *, startup=None):
        target = snapshot.portfolio_id
        if target not in self.targets:
            return
        old = self.snapshots.get(target)
        if old and old.checked_at > snapshot.checked_at:
            return  # Old direct-mode templates must not overwrite newer reads.
        if old == snapshot and startup is None:
            return
        self.snapshots[target] = snapshot
        if startup is not None or old is None or snapshot.checked_at > old.checked_at:
            self.startup[target] = bool(startup)
        self._render(target)

    def update_balance(self, balance: str):
        self.balance = balance
        for target in self.targets:
            self._render(target)

    def set_model(self, copy_model: str):
        self.copy_model = copy_model
        for target in self.targets:
            self._render(target)

    def _render(self, target):
        snapshot = self.snapshots.get(target)
        name, capacity, minimum = "待查询", "—", "—"
        if snapshot:
            name = snapshot.nickname
            capacity = f"{snapshot.copiers}/{snapshot.capacity}" + ("（启动）" if self.startup.get(target) else "")
            amount = snapshot.minimum_for(self.copy_model)
            minimum = f"{format(amount, 'f')} USDT" if amount is not None else "未知"
        self.tree.item(target, values=(target, name, capacity, minimum,
                                      f"{self.balance} USDT" if self.balance is not None else "—"))

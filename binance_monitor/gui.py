"""Compact Windows configuration and continuous copy-trading monitor."""
from __future__ import annotations

import ctypes
import math
import queue
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from .browser_account import BrowserAccount, BrowserClient
from .core import Event, Monitor, RequestFailure, Settings, ValidationError, detect_local_proxy, parse_targets, positive_decimal, validate_proxy
from .settings_view import settings_tab
from .storage import DATA_DIR, get_logger, load_config, save_config
from .success_view import SuccessNotifications
from .target_view import TargetTable
from .trade_panel import TradePanel
from .trading import AutoJoiner, DemoTradeClient, JoinPolicy, Journal, pending_records, review_pending


DEFAULT_TARGET = "5075281354358777856"
MODEL_LABELS = {"等比跟单": "FIXED_RATIO", "定额跟单（每笔10 USDT）": "FIXED_AMT"}
BACKGROUND = "#F0F0F0"
TEXT = "#202020"


def make_window(*, demo=False, quit_after_demo=False):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    App(root, demo=demo, quit_after_demo=quit_after_demo)
    return root


class App:
    def __init__(self, root, *, demo=False, quit_after_demo=False):
        self.root = root
        self.events = queue.Queue()
        self.worker = None
        self.monitor = None
        self.settings = None
        self.closing = False
        self.account_busy = False
        self.halted = False
        self.idle_status = "就绪"
        self.auto_active = False
        self.cooldown_until = 0.0
        self.join_status = {}
        self.confirmation_progress = {}
        self.snapshots = {}
        self.request_count = 0
        self.demo_dir = None
        self.quit_after_demo = quit_after_demo
        self.journal_path = DATA_DIR / "joins.sqlite3"
        self.unconfirmed = []
        self._layout_job = None
        self._split_initialized = False
        self.success_notifications = SuccessNotifications(root)
        self.logger = get_logger()
        root.title("币安合约跟单 · 全天抢位")
        root.geometry("1140x760")
        root.minsize(950, 640)
        root.configure(bg=BACKGROUND)
        root.protocol("WM_DELETE_WINDOW", self.close)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=9)
        style = self.style = ttk.Style(root)
        style.theme_use("clam")
        for name in ("TFrame", "Panel.TFrame", "TLabel", "Panel.TLabel", "TCheckbutton"):
            style.configure(name, background=BACKGROUND)
        for name in ("TLabel", "Panel.TLabel", "TCheckbutton"):
            style.configure(name, foreground=TEXT)
        for name in ("Muted.TLabel", "PanelMuted.TLabel"):
            style.configure(name, background=BACKGROUND, foreground="#595959")
        style.configure("Notice.TLabel", background=BACKGROUND, foreground="#806215")
        style.configure("Real.TCheckbutton", background=BACKGROUND, foreground="#934238")
        style.configure("TButton", padding=(12, 5), background="#E5E5E5", foreground=TEXT)
        style.map("TButton", background=[("active", "#DADADA")])
        style.configure("Primary.TButton", padding=(12, 5), background="#E5E5E5", foreground=TEXT)
        style.configure("TEntry", padding=4, fieldbackground="#FFFFFF")
        style.configure("TNotebook", background=BACKGROUND)
        style.configure("TNotebook.Tab", padding=(8, 4))

        container = self.container = ttk.Frame(root, padding=8)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)
        self.notebook = ttk.Notebook(container)
        self.notebook.grid(row=0, column=0, sticky="ew")
        config = settings_tab(self.notebook, "监控配置")
        config.columnconfigure(1, weight=4)
        config.columnconfigure(3, weight=1)
        ttk.Label(config, text="带单员分享链接（每行一个，可填写详情链接或 Portfolio ID）", style="Panel.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.targets = tk.Text(config, height=5, wrap="none", font=("Consolas", 10), bg="white", fg=TEXT,
                               relief="sunken", bd=1, padx=4, pady=4, undo=True)
        self.targets.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 14))
        self.targets.bind("<Tab>", lambda event: self._move_focus(event, forward=True))
        self.targets.bind("<Shift-Tab>", lambda event: self._move_focus(event, forward=False))
        self.interval = tk.StringVar(value="0.1")
        self.ceiling = tk.StringVar(value="300")
        self.proxy = tk.StringVar()
        self.copy_model = tk.StringVar(value="等比跟单")
        self.real = tk.BooleanVar(value=True)
        self.direct = tk.BooleanVar(value=True)
        self.sound = tk.BooleanVar(value=True)
        self.keep_awake = tk.BooleanVar(value=True)
        ttk.Label(config, text="抢位间隔（秒）", style="Panel.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12))
        self.interval_entry = ttk.Entry(config, textvariable=self.interval)
        self.interval_entry.grid(row=2, column=1, sticky="ew", padx=(0, 24))
        ttk.Label(config, text="最低金额安全上限（USDT）", style="Panel.TLabel").grid(row=2, column=2, sticky="w", padx=(0, 12))
        self.ceiling_entry = ttk.Entry(config, textvariable=self.ceiling, width=12)
        self.ceiling_entry.grid(row=2, column=3, sticky="ew")
        ttk.Label(config, text="HTTP/HTTPS 代理（可选）", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0), padx=(0, 12))
        self.proxy_entry = ttk.Entry(config, textvariable=self.proxy)
        self.proxy_entry.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(config, text="跟单模式", style="Panel.TLabel").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.model_combo = ttk.Combobox(config, textvariable=self.copy_model, values=tuple(MODEL_LABELS), state="readonly")
        self.model_combo.grid(row=4, column=1, sticky="ew", pady=(8, 0), padx=(0, 24))
        self.real_check = ttk.Checkbutton(config, variable=self.real, style="Real.TCheckbutton",
            text="我确认开启真实资金自动加入（平台最低金额，跟随带单员杠杆/保证金模式）")
        self.real_check.grid(row=5, column=0, columnspan=4, sticky="w", pady=(14, 4))
        self.direct_check = ttk.Checkbutton(config, variable=self.direct,
            text="直接抢加入（启动时校验金额和余额，之后不查空位，直接循环提交创建请求）")
        self.direct_check.grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 4))
        ttk.Label(config, text="抢位与正常核对均使用此间隔；超时或限流会等待。上次提交结果未确认时，暂停抢位、只核对结果。",
                  style="Notice.TLabel", wraplength=860).grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.trade = TradePanel(self, self.notebook)

        actions = self.actions = ttk.Frame(container)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 12))
        self.save_button = ttk.Button(actions, text="保存配置", command=self.save)
        self.save_button.pack(side="left", padx=(0, 8))
        self.test_button = ttk.Button(actions, text="测试连接和余额", command=self.test_connection)
        self.test_button.pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(actions, text="开始监控", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.status = tk.StringVar(value="尚未开始")
        self.status_label = ttk.Label(actions, textvariable=self.status, style="Muted.TLabel")
        self.status_label.pack(side="right")
        self.output_split = tk.PanedWindow(container, orient="vertical", background=BACKGROUND,
            borderwidth=0, sashwidth=8, sashpad=2, sashrelief="raised", showhandle=True,
            handlesize=8, handlepad=12, sashcursor="sb_v_double_arrow", opaqueresize=True)
        self.output_split.grid(row=2, column=0, sticky="nsew")
        self.target_table = TargetTable(self.output_split)
        self.output_split.add(self.target_table, minsize=100, stretch="never")
        self.model_combo.bind("<<ComboboxSelected>>", lambda _event: self.target_table.set_model(
            MODEL_LABELS.get(self.copy_model.get(), "FIXED_RATIO")))
        logs = self.results = ttk.Frame(self.output_split)
        self.output_split.add(logs, minsize=180, stretch="always")
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(1, weight=1)
        ttk.Label(logs, text="运行日志（同时写入 logs 目录）").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.recovery_button = ttk.Button(logs, text="核对未确认记录", command=self.review_records)
        self.recovery_button.grid(row=0, column=0, sticky="e", pady=(0, 6))
        self.recovery_button.grid_remove()
        self.log = tk.Text(logs, height=10, wrap="none", state="disabled", bg="#080808", fg="#E8E8E8",
                           font=("Consolas", 10), relief="sunken", bd=1, padx=5, pady=5)
        self.log.grid(row=1, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(logs, orient="vertical", command=self.log.yview)
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(logs, orient="horizontal", command=self.log.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.log.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.log.tag_configure("error", foreground="#FFB0A8")
        self.notebook.bind("<<NotebookTabChanged>>", self._schedule_layout)
        self.notebook.bind("<<SettingsContentChanged>>", self._schedule_layout)
        root.bind("<Configure>", self._window_resized, add=True)
        self._load()
        self.target_table.set_targets(parse_targets(self.targets.get("1.0", "end")))
        self.target_table.set_model(MODEL_LABELS[self.copy_model.get()])
        self._refresh_recovery()
        self._schedule_layout()
        self._append("就绪。默认真实资金、平台最低金额、持续抢位；请先在“本机登录”检测账户，再点击“开始监控”。")
        root.after(100, self.poll)
        if demo:
            root.after(500, lambda: self.start(demo=True))

    def _window_resized(self, event):
        if event.widget is self.root:
            self._schedule_layout()

    def _schedule_layout(self, _event=None):
        if self._layout_job is None:
            self._layout_job = self.root.after_idle(self._fit_settings)

    def _fit_settings(self):
        self._layout_job = None
        selected = self.notebook.select()
        if not selected:
            return
        tab = self.root.nametowidget(selected)
        chrome = max(26, self.notebook.winfo_reqheight() - int(self.notebook.cget("height") or 0))
        table_height = self.target_table.winfo_reqheight()
        row_height = int(self.style.lookup("Targets.Treeview", "rowheight"))
        table_minimum = table_height - (int(self.target_table.tree.cget("height")) - 1) * row_height
        self.output_split.paneconfigure(self.target_table, minsize=max(90, table_minimum))
        log_line_height = tkfont.Font(font=self.log.cget("font")).metrics("linespace")
        log_minimum = max(120, 3 * log_line_height + self.recovery_button.winfo_reqheight() + 35)
        self.output_split.paneconfigure(self.results, minsize=log_minimum)
        if not self._split_initialized:
            self.output_split.paneconfigure(self.target_table, height=table_height)
            self._split_initialized = True
        available = (self.container.winfo_height() - self.actions.winfo_reqheight()
                     - table_height - 12 - 22 - 180 - chrome - 16)
        height = max(100, min(tab.content.winfo_reqheight(), available))
        if int(self.notebook.cget("height") or 0) != height:
            self.notebook.configure(height=height)

    def _load(self):
        saved = {}
        try:
            saved = load_config()
            current = saved.get("version") == 2
            targets = saved.get("targets", [DEFAULT_TARGET])
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                raise ValueError
            value = Settings(parse_targets("\n".join(targets)), float(saved.get("interval", .1) if current else .1),
                             positive_decimal(saved.get("ceiling", "300")), validate_proxy(saved.get("proxy", detect_local_proxy())))
        except (ValueError, TypeError, OSError):
            value = Settings((DEFAULT_TARGET,), interval=.1, proxy=detect_local_proxy())
            self._append("配置文件无法读取；已使用默认值，请检查并重新保存。", "error")
        self.targets.insert("1.0", "\n".join(value.targets))
        self.interval.set(f"{value.interval:g}")
        self.ceiling.set(str(value.ceiling))
        self.proxy.set(value.proxy)
        if saved.get("version") == 2:
            self.real.set(saved.get("real", True) is True)
            self.direct.set(saved.get("direct", True) is True)
        legacy_trade = saved.get("trade", {})
        legacy_values = legacy_trade.get("values", {}) if isinstance(legacy_trade, dict) else {}
        old_model = legacy_values.get("model", "等比跟单") if isinstance(legacy_values, dict) else "等比跟单"
        if not isinstance(old_model, str):
            old_model = "等比跟单"
        model = saved.get("copy_model", "FIXED_AMT" if old_model.startswith("定额") else "FIXED_RATIO")
        self.copy_model.set(next((label for label, code in MODEL_LABELS.items() if code == model), "等比跟单"))
        self.trade.load(saved.get("trade", {}))

    def _move_focus(self, event, *, forward):
        widget = event.widget.tk_focusNext() if forward else event.widget.tk_focusPrev()
        if widget:
            widget.focus_set()
        return "break"

    def _settings(self, *, demo=False):
        try:
            interval = float(self.interval.get())
            model = MODEL_LABELS[self.copy_model.get()]
        except (ValueError, KeyError):
            raise ValidationError("请检查抢位间隔和跟单模式。") from None
        return Settings(parse_targets(self.targets.get("1.0", "end")), interval,
                        positive_decimal(self.ceiling.get()), validate_proxy(self.proxy.get()), True, demo, model, True)

    def _policy(self, settings):
        return JoinPolicy(None, settings.ceiling, copy_model=settings.copy_model,
                          cost_per_order="10" if settings.copy_model == "FIXED_AMT" else None,
                          direct_retry=self.direct.get(), max_attempts=None)

    def save(self):
        try:
            settings = self._settings()
            save_config({"version": 2, "targets": list(settings.targets), "interval": settings.interval,
                         "ceiling": str(settings.ceiling), "proxy": settings.proxy, "copy_model": settings.copy_model,
                         "real": self.real.get(), "direct": self.direct.get(), "trade": self.trade.config()})
        except (ValidationError, OSError) as error:
            messagebox.showerror("配置未保存", str(error), parent=self.root)
            return
        self._append("配置已保存。")
        self.target_table.set_targets(settings.targets)
        self.target_table.set_model(settings.copy_model)
        self._schedule_layout()

    def _editing(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (self.targets, self.interval_entry, self.ceiling_entry, self.proxy_entry,
                       self.real_check, self.direct_check, self.start_button, self.test_button, self.save_button,
                       self.recovery_button):
            widget.configure(state=state)
        self.model_combo.configure(state="readonly" if enabled else "disabled")
        self.stop_button.configure(state="disabled" if enabled else "normal")
        self.trade.editing(enabled)

    def start(self, *, demo=False):
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self._settings(demo=demo)
            joiner = None
            if demo:
                if self.demo_dir:
                    self.demo_dir.cleanup()
                self.demo_dir = tempfile.TemporaryDirectory(prefix="binance-monitor-demo-")
                # Demo has no authenticated browser or live transport, even with real checked.
                settings = Settings(settings.targets, max(.1, settings.interval), proxy=settings.proxy, demo=True)
                joiner = AutoJoiner(DemoTradeClient(), JoinPolicy(None, "300", max_attempts=None),
                                   Journal(Path(self.demo_dir.name) / "demo.sqlite3", "demo"), self.events.put)
            elif self.real.get():
                client = self.trade.client(settings.proxy)
                joiner = AutoJoiner(client, self._policy(settings),
                                   Journal(DATA_DIR / "joins.sqlite3", client.session.fingerprint), self.events.put)
        except (ValidationError, OSError) as error:
            messagebox.showerror("请检查配置", str(error), parent=self.root)
            return
        self.settings = settings
        self.target_table.set_targets(settings.targets, reset=True)
        self.target_table.set_model(settings.copy_model)
        self._schedule_layout()
        self.snapshots.clear()
        self.join_status.clear()
        self.confirmation_progress.clear()
        self.request_count = 0
        self.cooldown_until = 0
        self.halted = False
        self.auto_active = joiner is not None
        self._editing(False)
        self.status.set("启动核验中")
        if demo:
            self._append("离线演示开始，不连接币安，不使用真实资产。")
        elif joiner:
            self._append(f"启动监控 | {len(settings.targets)} 个目标 | 间隔 {settings.interval:g} 秒 | "
                         f"最低金额安全上限 {settings.ceiling} USDT | 平台最低投入 | 杠杆/保证金跟随带单员。")
        else:
            self._append("真实资金已取消勾选；只监控空位，不提交跟单。")
        self.monitor = Monitor(settings, self.events.put, joiner=joiner)
        self.worker = threading.Thread(target=self.monitor.run, name="slot-monitor", daemon=True)
        self.worker.start()

    def stop(self):
        if self.monitor:
            self.monitor.stop.set()
            self.stop_button.configure(state="disabled")
            self.status.set("正在停止，等待请求核对")

    def _append(self, message, tag=""):
        if self.settings and self.settings.demo:
            message = "[演示] " + message
        severity = "ERROR" if tag == "error" else "INFO"
        self.logger.log(40 if tag == "error" else 20, message)
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S} [{severity}] {message}\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 2000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.snapshot:
                self.snapshots[event.target] = event.snapshot
                self.target_table.update_snapshot(event.snapshot,
                    startup=event.message == "启动" if event.kind == "target_detail" else None)
            if event.kind == "checking":
                self.request_count += 1
            elif event.kind == "available":
                if not self.auto_active:
                    self._append(f"发现空位：{event.snapshot.nickname}({event.target})；真实资金未启用，未提交。")
            elif event.kind == "error":
                if self.account_busy:
                    self.trade.account_status.set("账户验证未通过 · 请查看下方日志")
                self._append((event.target + "：" if event.target else "") + event.message, "error")
            elif event.kind in {"joining", "join_full"}:
                self.join_status[event.target] = "直接抢位中" if self.direct.get() else "等待空位中"
            elif event.kind == "verifying":
                self.join_status[event.target] = "结果核对中"
                self.cooldown_until = 0
                self._append(f"{event.target} | {event.message}")
            elif event.kind in {"confirmation_checking", "confirmation_schedule"}:
                self.join_status[event.target] = "结果核对中"
                if event.progress:
                    self.confirmation_progress[event.target] = event.progress
            elif event.kind in {"confirmation_waiting", "confirmation_paused", "join_summary"}:
                self.join_status[event.target] = "结果核对中" if event.kind == "confirmation_waiting" else "已暂停" if event.kind == "confirmation_paused" else "已停止"
                if event.kind == "confirmation_waiting" and event.progress:
                    self.confirmation_progress[event.target] = event.progress
                elif event.kind != "confirmation_waiting":
                    self.confirmation_progress.pop(event.target, None)
                self._append(event.message)
            elif event.kind == "join_progress":
                self.join_status[event.target] = "直接抢位中" if self.direct.get() else "等待空位中"
                self._append(event.message)
            elif event.kind == "joined":
                self.confirmation_progress.pop(event.target, None)
                self.join_status[event.target] = "加入成功"
                if event.confirmation and event.snapshot:
                    mode = {"FIXED_RATIO": "等比", "FIXED_AMT": "定额"}.get(event.confirmation.copy_model, "历史模式未记录")
                    self._append(f"已成功加入 {event.snapshot.nickname} | Portfolio ID: {event.target} | "
                                 f"模式: {mode} | 保证金: {format(event.confirmation.amount, 'f')} USDT")
                else:
                    self._append(event.message)
                if self.settings and self.settings.sound:
                    self.root.bell()
                if not self.closing:
                    try:
                        self.success_notifications.show(event, demo=bool(self.settings and self.settings.demo))
                    except tk.TclError:
                        self._append("成功提示未能显示，结果已记录在日志中。", "error")
            elif event.kind == "joined_existing":
                self.confirmation_progress.pop(event.target, None)
                self.join_status[event.target] = "已在跟单"
                self._append(f"{event.target} | 官方已显示正在跟单，跳过重复加入。")
            elif event.kind == "join_skipped":
                self._append(f"{event.target} | {event.message}")
            elif event.kind == "balance":
                self.target_table.update_balance(event.message)
                self.trade.account_status.set(f"登录查询成功 · 现货可用 {event.message} USDT")
                self._append(f"现货可用余额：{event.message} USDT。")
            elif event.kind == "browser_open":
                self.trade.account_status.set("专用 Edge 已打开 · 登录完成后点击“检测账户”")
                self._append(event.message)
            elif event.kind == "browser_ready":
                self.trade.browser_identity = self.trade.browser_account.identity if self.trade.browser_account else None
                self._append(event.message)
            elif event.kind == "browser_closed":
                self.trade.browser_identity = None
                self.trade.account_status.set("专用 Edge 已关闭 · 再次使用时点击“打开 Edge 登录”")
                self._append(event.message)
            elif event.kind in {"preflight_ok", "connection_ok", "records_checked", "all_joined", "power", "notice"}:
                if event.message:
                    self._append(event.message)
            elif event.kind == "cooldown":
                self.cooldown_until = time.monotonic() + event.retry_after
                if event.message:
                    self._append(event.message)
            elif event.kind == "halted":
                self.halted = True
                self._append(event.message, "error")
            elif event.kind == "stopped":
                self.confirmation_progress.clear()
                self.idle_status = "已停止"
                self._append("监控已停止。")
            elif event.kind == "account_done":
                self.idle_status = "账户操作已结束"
                self._append("账户操作已结束，请查看上方检测结果。")
        alive = self.worker is not None and self.worker.is_alive()
        if self.worker is not None and not alive:
            self.worker = None
            self.monitor = None
            self.account_busy = False
            self._editing(True)
            self._refresh_recovery()
            if self.quit_after_demo and self.settings and self.settings.demo:
                self.closing = True
        if self.halted:
            self.status.set("异常停止，请查看日志")
        elif self.account_busy:
            self.status.set("正在查询账户")
        elif not alive:
            self.status.set(self.idle_status)
        elif self.monitor and self.monitor.stop.is_set():
            self.status.set("正在停止，等待请求核对")
        elif "结果核对中" in self.join_status.values():
            self.status.set(self._confirmation_status())
        elif self.cooldown_until > time.monotonic():
            self.status.set(f"冷却中，{max(1, int(self.cooldown_until - time.monotonic()))} 秒后恢复")
        else:
            self.status.set("演示中" if self.settings and self.settings.demo else "全天抢位中" if self.auto_active else "监控中")
        if self.closing and not alive:
            self._close_browser()
            if self.demo_dir:
                self.demo_dir.cleanup()
            self.root.destroy()
            return
        self.root.after(150, self.poll)

    def _confirmation_status(self):
        active = [value for target, value in self.confirmation_progress.items()
                  if self.join_status.get(target) == "结果核对中"]
        if not active:
            return "结果核对中 · 不重复提交"
        querying = [value for value in active if value.query_started_at is not None]
        if querying:
            current = querying[-1]
            elapsed = max(0, int(time.monotonic() - current.query_started_at))
            return f"核对第 {current.queries + 1} 次 · 已用 {elapsed} 秒"
        current = min(active, key=lambda value: value.next_query_at)
        remaining = max(0, current.next_query_at - time.monotonic())
        remaining_text = f"{max(1, math.ceil(remaining * 10 - 1e-8)) / 10:.1f}" if 0 < remaining < 1 else str(math.ceil(remaining))
        return f"核对 {current.queries} 次 · {remaining_text} 秒后查" if remaining else f"核对 {current.queries} 次 · 等待查询"

    def _refresh_recovery(self):
        try:
            self.unconfirmed = pending_records(self.journal_path)
        except RequestFailure as error:
            self._append(str(error), "error")
            return
        if self.unconfirmed:
            self.recovery_button.grid()
        else:
            self.recovery_button.grid_remove()

    def review_records(self):
        if self.worker and self.worker.is_alive():
            return
        self._refresh_recovery()
        if not self.unconfirmed:
            return
        window = tk.Toplevel(self.root)
        window.title("核对未确认记录")
        window.transient(self.root)
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="选择上次未确认的提交：").pack(anchor="w")
        labels = [f"{row['target']}  |  {row['amount']} USDT" for row in self.unconfirmed]
        selection = ttk.Combobox(frame, values=labels, state="readonly", width=55)
        selection.current(0)
        selection.pack(fill="x", pady=(8, 12))
        ttk.Label(frame, text="可先仅查询官方状态。持续自动核对请关闭此窗口，点击“开始监控”。\n"
                  "只有在官方“我的跟单”和资产流水均确认未创建、无扣款，\n原请求也已结束后，才可手动解除锁定。",
                  wraplength=500, justify="left").pack(anchor="w")
        records = list(self.unconfirmed)

        def review(manual_release=False):
            record = records[selection.current()]
            if manual_release and not messagebox.askyesno(
                    "确认已完成人工核对", f"组合 {record['target']}，原投入 {record['amount']} USDT。\n\n"
                    "你是否已经在币安官方页面核对：\n"
                    "• “我的跟单”中没有此组合；\n• 资产流水中没有此次跟单的扣款；\n• 原请求已结束。\n\n"
                    "确认后程序会重新查询官方状态，再处理记录。\n解除锁定后不会自动提交，需你再次点击“开始监控”。",
                    parent=window, default=messagebox.NO):
                return
            window.destroy()
            def action(client, _settings):
                journal = Journal(self.journal_path, client.session.fingerprint)
                review_pending(client, journal, record['target'], record['operation_id'], self.events.put,
                               manual_release=manual_release)
            self._account_task(action)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="仅查询官方状态", command=review).pack(side="left")
        ttk.Button(buttons, text="确认未创建并解除锁定", command=lambda: review(True)).pack(side="right")
        window.grab_set()
        selection.focus_set()

    def _account_task(self, action):
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self._settings()
            client = self.trade.client(settings.proxy)
        except ValidationError as error:
            messagebox.showerror("请检查本机登录", str(error), parent=self.root)
            return
        self.account_busy = True
        self.target_table.set_targets(settings.targets, reset=True)
        self.target_table.set_model(settings.copy_model)
        self._schedule_layout()
        self.halted = False
        self._editing(False)
        self.stop_button.configure(state="disabled")
        self.status.set("正在查询账户")

        def run():
            try:
                action(client, settings)
            except Exception as error:
                message = str(error) if isinstance(error, RequestFailure) else "本机账户查询失败，请检查配置后重试。"
                self.events.put(Event("error", message=message))
            finally:
                self.events.put(Event("account_done"))

        self.worker = threading.Thread(target=run, name="account-check", daemon=True)
        self.worker.start()

    def test_connection(self):
        def action(client, settings):
            balance = client.balance()
            self.events.put(Event("balance", message=str(balance)))
            for target in settings.targets:
                detail = client.detail(target)
                self.events.put(Event("connection_ok", target, detail.snapshot, message=f"{target}：登录详情读取成功，" +
                                      (f"已跟单，组合 {detail.copy_id}。" if detail.has_copy else "尚未跟单。")))
        self._account_task(action)

    def _browser_task(self, action, *, targets=None):
        if self.worker and self.worker.is_alive():
            return
        try:
            proxy = validate_proxy(self.proxy.get().strip())
        except ValidationError as error:
            messagebox.showerror("请检查代理", str(error), parent=self.root)
            return
        self.trade.mode.set("browser")
        self.target_table.set_targets(targets if targets is not None else self.target_table.targets, reset=True)
        self.target_table.set_model(MODEL_LABELS.get(self.copy_model.get(), "FIXED_RATIO"))
        self._schedule_layout()
        self.account_busy = True
        self.halted = False
        self._editing(False)
        self.stop_button.configure(state="disabled")
        self.trade.account_status.set("正在连接专用 Edge…")
        self.status.set("正在连接 Edge")
        def run():
            try:
                action(proxy)
            except Exception as error:
                message = str(error) if isinstance(error, (RequestFailure, ValidationError)) else "专用 Edge 操作未完成，请重新打开登录窗口后检测。"
                self.events.put(Event("error", message=message))
            finally:
                self.events.put(Event("account_done"))
        self.worker = threading.Thread(target=run, name="browser-account-check", daemon=True)
        self.worker.start()

    def open_browser_login(self):
        def action(proxy):
            if self.trade.browser_account is None:
                self.trade.browser_account = BrowserAccount(DATA_DIR / "edge-profile")
            self.trade.browser_identity = None
            self.trade.browser_account.open(proxy)
            self.events.put(Event("browser_open", message="专用 Edge 已打开，请在币安官方页面登录，再回程序检测账户。"))
        self._browser_task(action)

    def detect_browser_account(self):
        try:
            targets = self._settings().targets
        except ValidationError as error:
            messagebox.showerror("请检查组合", str(error), parent=self.root)
            return
        def action(_proxy):
            account = self.trade.browser_account
            if account is None:
                raise ValidationError("请先点击“打开 Edge 登录”，在该窗口完成登录。")
            self.trade.browser_identity = None
            identity = account.detect()
            client = BrowserClient(account, identity)
            balance = client.balance()
            self.events.put(Event("browser_ready", message="已在专用 Edge 确认登录账号。"))
            self.events.put(Event("balance", message=str(balance)))
            for target in targets:
                detail = client.detail(target)
                self.events.put(Event("connection_ok", target, detail.snapshot, message=f"{target}：浏览器登录详情读取成功，" + ("已在跟单。" if detail.has_copy else "尚未跟单。")))
        self._browser_task(action, targets=targets)

    def close_browser_login(self):
        def action(_proxy):
            if self.trade.browser_account is not None:
                self.trade.browser_account.close()
                self.trade.browser_account = None
            self.events.put(Event("browser_closed", message="已关闭专用 Edge；登录状态由该浏览器的本机配置保留。"))
        self._browser_task(action)

    def _close_browser(self):
        if self.trade.browser_account is not None:
            self.trade.browser_account.close()
            self.trade.browser_account = None

    def close(self):
        self.closing = True
        self.stop()
        if self.worker is None or not self.worker.is_alive():
            self._close_browser()
            if self.demo_dir:
                self.demo_dir.cleanup()
            self.root.destroy()

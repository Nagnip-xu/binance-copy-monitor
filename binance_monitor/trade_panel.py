from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .core import ValidationError
from .browser_account import BrowserClient
from .credentials import import_request, load_session, save_session
from .private_api import BalanceQuery
from .storage import DATA_DIR
from .settings_view import settings_tab


class TradePanel:
    def __init__(self, app, notebook: ttk.Notebook):
        self.app = app
        self.session = None
        self.balance_query = None
        self.browser_account = None
        self.browser_identity = None
        self.mode = tk.StringVar(value="browser")
        self.persist = tk.BooleanVar(value=False)
        self.widgets = []
        self.readonly_widgets = []
        account = self._tab(notebook, "本机登录")
        ttk.Label(account, text="点击“打开 Edge 登录”，在专用窗口中登录币安，然后回到这里点击“检测账户”。\n首次需要重新登录；以后由 Edge 保留登录状态。运行期间保持该窗口打开。", style="PanelMuted.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        browser_buttons = ttk.Frame(account, style="Panel.TFrame")
        browser_buttons.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(14, 12))
        for label, command in (("打开 Edge 登录", app.open_browser_login), ("检测账户", app.detect_browser_account),
                               ("关闭专用窗口", app.close_browser_login)):
            button = ttk.Button(browser_buttons, text=label, command=command,
                                style="Primary.TButton" if label == "打开 Edge 登录" else "TButton")
            button.pack(side="left", padx=(0, 8))
            self.widgets.append(button)
        self.account_status = tk.StringVar(value="尚未连接 · 请打开专用 Edge 登录")
        ttk.Label(account, textvariable=self.account_status, style="Panel.TLabel", wraplength=820).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(account, text="密码与验证在币安官方页面完成。检测账户只查询，不发起跟单。", style="PanelMuted.TLabel").grid(
            row=3, column=0, columnspan=4, sticky="w")
        advanced = ttk.Button(account, text="手动导入请求（高级）", command=self.toggle_manual)
        advanced.grid(row=4, column=0, columnspan=4, sticky="w", pady=(12, 0))
        self.widgets.append(advanced)
        manual = self.manual = ttk.Frame(account, style="Panel.TFrame")
        manual.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        manual.columnconfigure(0, weight=1)
        ttk.Label(manual, text="粘贴一条最新的现货 get-user-asset 请求，登录与余额一起导入。", style="PanelMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.import_text = tk.Text(manual, height=3, wrap="word", font=("Consolas", 9), undo=False,
                                   bg="#FFFFFF", fg="#18212B", relief="solid", bd=1, padx=8, pady=6)
        self.import_text.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        self.import_text.bind("<Tab>", lambda event: app._move_focus(event, forward=True))
        self.import_text.bind("<Shift-Tab>", lambda event: app._move_focus(event, forward=False))
        self.widgets.append(self.import_text)
        buttons = ttk.Frame(manual, style="Panel.TFrame")
        buttons.grid(row=2, column=0, sticky="ew")
        for label, command in (("导入并测试", self.import_and_test), ("重新测试", app.test_connection),
                               ("清除本机登录", self.clear)):
            button = ttk.Button(buttons, text=label, command=command,
                                style="Primary.TButton" if label == "导入并测试" else "TButton")
            button.pack(side="left", padx=(0, 8))
            self.widgets.append(button)
        self._check(manual, "手动导入时用 Windows 当前用户加密保存参数", self.persist, 3)
        open_page = ttk.Button(manual, text="打开普通浏览器资产页", command=lambda: webbrowser.open("https://www.binance.com/zh-CN/my/wallet/account/main", new=2))
        open_page.grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.widgets.append(open_page)
        manual.grid_remove()

    def toggle_manual(self):
        if self.manual.winfo_manager():
            self.manual.grid_remove()
            self.mode.set("browser")
        else:
            self.manual.grid()
            self.mode.set("manual")
        self._status()

    def client(self, proxy):
        if self.mode.get() == "browser":
            if self.browser_account is None or self.browser_identity is None:
                raise ValidationError("请先打开专用 Edge 登录，再点击“检测账户”。")
            return BrowserClient(self.browser_account, self.browser_identity)
        from .private_api import PrivateClient
        session, query = self.require_account()
        return PrivateClient(session, query, proxy)

    def _tab(self, notebook, title):
        frame = settings_tab(notebook, title)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        return frame

    def _check(self, frame, text, variable, row):
        check = ttk.Checkbutton(frame, text=text, variable=variable)
        check.grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 4))
        self.widgets.append(check)

    def editing(self, enabled: bool):
        for widget in self.widgets:
            widget.configure(state="normal" if enabled else "disabled")
        for widget in self.readonly_widgets:
            widget.configure(state="readonly" if enabled else "disabled")

    def config(self):
        query = self.balance_query
        return {"balance_query": None if query is None else {"url": query.url, "method": query.method, "body": query.body}}

    def load(self, saved):
        if not isinstance(saved, dict):
            return
        query = saved.get("balance_query")
        if isinstance(query, dict):
            try:
                self.balance_query = BalanceQuery(**query)
            except (ValidationError, TypeError):
                self.app._append("已忽略无效余额请求配置，请重新导入。", "error")
        path = DATA_DIR / "session.dpapi"
        if path.exists():
            try:
                self.session = load_session(path)
            except ValidationError:
                self.app._append("已保存登录参数无法解密，请重新导入。", "error")
        self._status()

    def _status(self):
        if self.mode.get() == "browser":
            self.account_status.set("Edge 账户已检测 · 可重新检测" if self.browser_identity else "尚未验证 · 请打开专用 Edge 登录并检测账户")
            return
        if self.session and self.balance_query:
            self.account_status.set("参数已导入 · 等待账户验证")
        else:
            self.account_status.set("等待导入 · 只需一条现货余额请求")

    def _import(self, *, balance: bool) -> bool:
        try:
            request = import_request(self.import_text.get("1.0", "end"))
            query = BalanceQuery.from_import(request) if balance else None
            if (not balance and self.balance_query is not None and self.session is not None
                    and request.session.fingerprint != self.session.fingerprint):
                raise ValidationError("余额请求已包含登录参数。为避免旧登录覆盖新登录，请直接导入最新的完整现货余额请求。")
            if self.persist.get():
                save_session(request.session, DATA_DIR / "session.dpapi")
            self.session = request.session
            self.mode.set("manual")
            if query:
                self.balance_query = query
            self.import_text.delete("1.0", "end")
            self._status()
            self.app._append("已在本机导入余额请求与登录参数。" if balance else "已在本机导入登录参数。")
            return True
        except (ValidationError, OSError) as error:
            messagebox.showerror("导入未完成", str(error), parent=self.app.root)
            return False

    def import_and_test(self):
        if self._import(balance=True):
            self.app.test_connection()

    def import_session(self):
        self._import(balance=False)

    def import_balance(self):
        self._import(balance=True)

    def clear(self):
        try:
            (DATA_DIR / "session.dpapi").unlink(missing_ok=True)
        except OSError:
            messagebox.showerror("清除失败", "无法删除本机加密登录文件，请检查文件权限。", parent=self.app.root)
            return
        self.session = None
        self.import_text.delete("1.0", "end")
        self._status()
        self.app._append("已清除内存与本机加密登录参数；再次运行前需要重新验证账户。")

    def require_account(self):
        if self.session is None or self.balance_query is None:
            raise ValidationError("请在“本机登录”中粘贴一条最新的现货余额请求，点击“导入并测试”。")
        return self.session, self.balance_query

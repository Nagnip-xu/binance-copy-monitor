"""Dedicated Edge login and same-origin requests, without exporting browser cookies.

The user signs in on Binance itself. Playwright owns only this application's
separate Edge profile, uses its normal browser session, and never drives login
forms, verification challenges, extensions, or the user's existing Edge profile.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .core import DETAIL_ENDPOINT, MAX_RESPONSE_BYTES, RequestFailure, ValidationError, retry_after_seconds, validate_proxy
from .credentials import ALLOWED_HEADERS, official_url
from .private_api import (BALANCE_PATHS, CREATE_PATH, SPOT_ASSET_V3_PATH, BalanceQuery,
                          JoinNotSent, JoinUncertain, PrivateClient, business_data)


ORIGIN = "https://www.binance.com"
WALLET_URL = ORIGIN + "/zh-CN/my/wallet/account/main"
IDENTITY_PATH = "/bapi/accounts/v1/private/account/get-user-base-info"
IDENTITY_URL = ORIGIN + IDENTITY_PATH
IDENTITY_CACHE_SECONDS = 5.0


@dataclass(frozen=True)
class BrowserIdentity:
    fingerprint: str = field(repr=False)

    def __repr__(self):
        return "BrowserIdentity(<redacted>)"


def account_identity(payload: object) -> BrowserIdentity:
    data = business_data(payload)
    if not isinstance(data, dict):
        raise RequestFailure("登录响应缺少账号标识，暂不能启用自动加入。", fatal=True)
    values = []
    # Only explicit account UID fields are identities; never device IDs, emails,
    # analytics cookies, or the generic id of an unrelated object.
    for node in (data, data.get("userInfo"), data.get("baseInfo")):
        if not isinstance(node, dict):
            continue
        for key in ("userId", "userID", "uid"):
            if key in node:
                raw = node[key]
                if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,29}", str(raw)):
                    raise RequestFailure("登录响应的账号标识无效，已停止账户操作。", fatal=True)
                values.append(str(raw))
    if not values or len(set(values)) != 1:
        raise RequestFailure("暂无法从登录响应确认唯一账号，请保留专用 Edge 窗口并重新检测。", fatal=True)
    return BrowserIdentity("browser-uid:" + hashlib.sha256(values[0].encode("ascii")).hexdigest())


# Evaluated in a Binance document. Credentials stay with the browser's cookie
# store. Same-origin and redirect checks apply before returning any response.
FETCH_JSON = r"""async ({url, method, body, headers, maximum, timeoutMs}) => {
  if (location.origin !== 'https://www.binance.com') return {error: 'login'};
  if (new URL(url).origin !== location.origin) return {error: 'origin'};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method, credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: {...headers, 'accept': 'application/json', 'content-type': 'application/json', 'clienttype': 'web'},
      ...(body === null ? {} : {body: JSON.stringify(body)}), signal: controller.signal
    });
    if (response.url !== url) return {error: 'redirect'};
    if (response.status !== 200) return {status: response.status, retryAfter: response.headers.get('retry-after')};
    const reader = response.body.getReader();
    const chunks = []; let total = 0;
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      total += chunk.value.length;
      if (total > maximum) {await reader.cancel(); return {error: 'large'};}
      chunks.push(chunk.value);
    }
    const joined = new Uint8Array(total); let offset = 0;
    for (const chunk of chunks) {joined.set(chunk, offset); offset += chunk.length;}
    try {return {status: 200, payload: JSON.parse(new TextDecoder().decode(joined))};}
    catch {return {error: 'json'};}
  } catch {return {error: controller.signal.aborted ? 'timeout' : 'network'};}
  finally {clearTimeout(timer);}
}"""


class BrowserAccount:
    def __init__(self, profile: Path):
        self.profile = profile.resolve()
        self.identity: BrowserIdentity | None = None
        self.balance_query = BalanceQuery(ORIGIN + SPOT_ASSET_V3_PATH, "POST",
                                          {"needBtcValuation": True, "quoteAsset": "USDT"})
        self._context = None
        self._page = None
        self._playwright = None
        self._headers = {}
        self._proxy = None
        self._loop = None
        self._ready = threading.Event()
        self._closed = False
        self._observations = set()
        self._verified_signature = None
        self._verified_at = 0.0
        self._clock = time.monotonic
        self._session_epoch = 0
        self._thread = threading.Thread(target=self._run, name="dedicated-edge", daemon=True)
        self._thread.start()

    def _run(self):
        loop = self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._gate = asyncio.Lock()
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _call(self, name: str, *args, timeout=35):
        if self._closed or not self._ready.wait(5):
            raise RequestFailure("专用 Edge 连接已关闭，请重新打开登录窗口。", fatal=True)

        async def serialized():
            async with self._gate:
                return await getattr(self, name)(*args)

        future = asyncio.run_coroutine_threadsafe(serialized(), self._loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise RequestFailure("专用 Edge 请求超时，请检查该窗口和网络连接。", fatal=True) from None
        except (RequestFailure, ValidationError):
            raise
        except Exception:
            # Playwright errors may contain URLs or response material.
            raise RequestFailure("专用 Edge 连接中断，请打开登录窗口后重新检测账户。", fatal=True) from None

    def open(self, proxy=""):
        self._call("_open", validate_proxy(proxy), timeout=50)

    async def _open(self, proxy):
        if self._context is not None and self._proxy == proxy and any(not p.is_closed() for p in self._context.pages):
            await self._context.pages[-1].bring_to_front()
            return
        await self._close_context()
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RequestFailure("此版本缺少 Edge 登录组件，请使用完整打包版本。", fatal=True) from None
        self.profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile), channel="msedge", headless=False,
                proxy={"server": proxy} if proxy else None,
                viewport=None, timeout=20000,
            )
        except Exception:
            await self._close_context()
            raise RequestFailure("无法打开专用 Edge。请确认已安装 Edge，且没有另一个程序占用同一登录窗口。", fatal=True) from None
        self._proxy = proxy
        self._context.on("response", self._response)
        self._context.on("request", self._session_request)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        try:
            await self._page.goto(WALLET_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            # Leave the visible browser open so the user can inspect proxy/login.
            raise RequestFailure("Edge 已打开，但币安页面未加载完成。请在该窗口检查网络，登录后点击“检测账户”。", fatal=True) from None

    def _response(self, response):
        if len(self._observations) >= 16:
            return
        task = asyncio.create_task(self._observe(response))
        self._observations.add(task)
        task.add_done_callback(self._observations.discard)

    def _invalidate_identity_cache(self):
        self._verified_signature = None
        self._verified_at = 0.0
        self._session_epoch += 1

    def _session_request(self, request):
        # Inspect only the route and method, never authentication form contents.
        parsed = urlsplit(request.url)
        if (parsed.hostname == "accounts.binance.com" or
                (parsed.path.startswith(("/bapi/accounts/", "/bapi/sub-account/"))
                 and parsed.path != IDENTITY_PATH and request.method not in {"GET", "HEAD"})):
            self._invalidate_identity_cache()

    async def _session_signature(self):
        if self._context is None:
            return None  # Without a browser session, verification cannot be cached.
        page = await self._official_page()
        try:
            cookies = await self._context.cookies(ORIGIN)
            # Only the digest survives this call. No cookie value is kept in
            # account fields, written to disk, logged, or exposed to the UI.
            values = sorted((cookie["name"], cookie["value"], cookie.get("domain", ""), cookie.get("path", ""))
                            for cookie in cookies)
            material = [id(self._context), id(page), page.url, self._session_epoch, values, self._headers]
            return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).digest()
        except Exception:
            raise RequestFailure("无法核对专用 Edge 的登录会话，请重新检测账户。", fatal=True) from None

    async def _verify_identity(self, identity, *, allow_cache=False):
        if self.identity != identity:
            self._invalidate_identity_cache()
            raise RequestFailure("专用 Edge 的账号已变化，已停止账户操作。请重新检测账户。", fatal=True)
        for _ in range(2):
            signature = await self._session_signature()
            if (allow_cache and signature is not None and signature == self._verified_signature
                    and 0 <= self._clock() - self._verified_at < IDENTITY_CACHE_SECONDS):
                return
            current = account_identity(await self._fetch(IDENTITY_URL))
            if self.identity != identity or current != identity:
                self.identity = None
                self._invalidate_identity_cache()
                raise RequestFailure("专用 Edge 的账号已变化，已停止账户操作。请重新检测账户。", fatal=True)
            after = await self._session_signature()
            if signature is not None and signature != after:
                self._invalidate_identity_cache()
                continue  # Session changed during the identity read: do not send yet.
            self._verified_signature = after
            self._verified_at = self._clock()
            return
        raise RequestFailure("登录会话正在变化，确认账号后自动恢复。")

    async def _observe(self, response):
        try:
            parsed = urlsplit(response.url)
            if (parsed.scheme != "https" or parsed.netloc != "www.binance.com"
                    or response.status != 200
                    or parsed.path not in BALANCE_PATHS | {IDENTITY_PATH, urlsplit(DETAIL_ENDPOINT).path}):
                return
            request = response.request
            # Keep only explicit, relevant headers in memory. Do not copy
            # Cookie headers, passwords or browser storage into this header set.
            names = sorted(ALLOWED_HEADERS - {"cookie", "user-agent", "accept-language"})
            values = await asyncio.gather(*(request.header_value(name) for name in names))
            selected = {key: value for key, value in zip(names, values)
                        if isinstance(value, str) and len(value) <= 100_000
                        and not any(c in value for c in ("\r", "\n", "\x00"))}
            if selected.get("csrftoken"):
                self._headers = selected
            if parsed.path in BALANCE_PATHS:
                try:
                    body = json.loads(request.post_data) if request.post_data else None
                    query = BalanceQuery(request.url, request.method, body)
                    if query.explicitly_spot:
                        self.balance_query = query
                except (ValueError, TypeError, ValidationError):
                    pass
        except Exception:
            pass

    async def _official_page(self):
        if self._context is None:
            raise RequestFailure("请先点击“打开 Edge 登录”。", fatal=True)
        pages = [p for p in self._context.pages if not p.is_closed()
                 and p.url.startswith(ORIGIN + "/")]
        if not pages:
            raise RequestFailure("请先在专用 Edge 完成币安登录，进入现货资产页后再检测。", fatal=True)
        self._page = pages[-1]
        return self._page

    async def _fetch(self, url, method="GET", body=None):
        official_url(url)
        page = await self._official_page()
        if not self._headers.get("csrftoken"):
            raise RequestFailure("请等待专用 Edge 的现货资产列表加载完成，再点击“检测账户”；必要时刷新该页面。", fatal=True)
        result = await page.evaluate(FETCH_JSON, {"url": url, "method": method, "body": body,
                                                "headers": dict(self._headers), "maximum": MAX_RESPONSE_BYTES,
                                                "timeoutMs": 8000 if urlsplit(url).path == CREATE_PATH else 4000})
        if not isinstance(result, dict):
            raise RequestFailure("浏览器查询未返回有效结果，请重新检测账户。", fatal=True)
        if result.get("status") == 401:
            raise RequestFailure("专用 Edge 的登录已失效，请在该窗口重新登录，再检测账户。", fatal=True)
        if result.get("status") == 429:
            delay = retry_after_seconds(result.get("retryAfter"))
            raise RequestFailure("币安暂时限制查询，冷却后自动恢复。", retry_after=delay, fatal=delay > 3600)
        if result.get("error") in {"network", "timeout"}:
            reason = "超时" if result["error"] == "timeout" else "连接中断"
            raise RequestFailure(f"浏览器请求{reason}，将自动查询恢复。")
        if result.get("status") in {500, 502, 503, 504}:
            raise RequestFailure(f"浏览器请求返回 HTTP {result['status']}，将自动查询恢复。")
        if result.get("status") != 200 or "payload" not in result:
            reason = {"json": "响应不是有效 JSON", "large": "响应过大", "redirect": "响应发生重定向",
                      "login": "页面已离开登录域", "origin": "请求来源校验未通过"}.get(result.get("error"), "响应格式异常")
            status = result.get("status")
            if type(status) is int and 100 <= status <= 599:
                reason = f"HTTP {status}"
            raise RequestFailure(f"浏览器请求未完成（{reason}），请在专用 Edge 检查登录或页面验证后重新检测。", fatal=True)
        return result["payload"]

    def detect(self) -> BrowserIdentity:
        return self._call("_detect")

    async def _detect(self):
        self.identity = None
        self._invalidate_identity_cache()
        self._headers.clear()
        page = await self._official_page()
        await page.goto(WALLET_URL, wait_until="domcontentloaded", timeout=20000)
        # Give the page's own read-only requests a chance to supply its headers.
        for _ in range(20):
            if self._headers.get("csrftoken"):
                break
            await asyncio.sleep(0.25)
        identity = account_identity(await self._fetch(IDENTITY_URL))
        self.identity = identity
        return identity

    def request(self, url, *, method="GET", body=None, identity: BrowserIdentity, joining=False):
        # Exactly the same parser/ledger rules apply as in manual mode.
        try:
            return self._call("_request", url, method, body, identity, joining)
        except JoinNotSent:
            raise
        except Exception as error:
            if joining:
                reason = str(error) if isinstance(error, RequestFailure) else "浏览器连接中断或响应处理失败。"
                raise JoinUncertain(f"创建请求结果待核对：{reason}",
                                    retry_after=error.retry_after if isinstance(error, RequestFailure) else 0) from None
            if isinstance(error, (RequestFailure, ValidationError)):
                raise
            raise RequestFailure("浏览器账户查询失败，请重新检测账户。", fatal=True) from None

    async def _request(self, url, method, body, identity, joining):
        official_url(url)
        path = urlsplit(url).path
        if joining:
            if path != CREATE_PATH or method != "POST" or not isinstance(body, dict):
                raise ValidationError("浏览器加入请求的地址或参数无效。")
        elif path in BALANCE_PATHS:
            BalanceQuery(url, method, body)
        elif path != urlsplit(DETAIL_ENDPOINT).path or method != "GET" or body is not None:
            raise ValidationError("浏览器只允许已配置的余额和跟单详情查询。")
        try:
            # Read-only balance/confirmation operations always verify afresh.
            # Repeated creates reuse verification for at most five seconds,
            # provided the entire browser session signature is unchanged.
            await self._verify_identity(identity, allow_cache=joining)
        except RequestFailure as error:
            if joining:
                raise JoinNotSent(str(error), fatal=error.fatal, retry_after=error.retry_after) from None
            raise
        return await self._fetch(url, method, body)

    async def _close_context(self):
        self.identity = None
        self._invalidate_identity_cache()
        self._headers.clear()
        self._proxy = None
        for task in list(self._observations):
            task.cancel()
        self._observations.clear()
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def close(self):
        if self._closed:
            return
        try:
            self._call("_close_context", timeout=10)
        except RequestFailure:
            pass
        finally:
            self._closed = True
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)


class BrowserClient(PrivateClient):
    def __init__(self, account: BrowserAccount, identity: BrowserIdentity):
        self.account = account
        self.session = identity
        self.balance_query = account.balance_query

    def _request(self, url: str, *, method="GET", body=None, joining=False):
        return self.account.request(url, method=method, body=body, identity=self.session, joining=joining)


def check_edge_component():
    """Packaging smoke check: temporary profile and local data page, no network."""
    from tempfile import TemporaryDirectory
    async def check():
        from playwright.async_api import async_playwright
        with TemporaryDirectory(prefix="binance-edge-component-") as profile:
            async with async_playwright() as runtime:
                context = await runtime.chromium.launch_persistent_context(profile, channel="msedge", headless=True, timeout=20000)
                try:
                    page = context.pages[0]
                    await page.goto("data:text/html,<title>Edge component check</title>")
                    if await page.title() != "Edge component check":
                        raise RuntimeError("Edge component unavailable")
                finally:
                    await context.close()
    asyncio.run(check())

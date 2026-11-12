from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .core import DETAIL_ENDPOINT, ID_PATTERN, MAX_RESPONSE_BYTES, RequestFailure, Snapshot, ValidationError, parse_snapshot, retry_after_seconds, validate_proxy
from .credentials import ImportedRequest, Session, official_url


CREATE_PATH = "/bapi/futures/v1/private/future/copy-trade/copy-portfolio/create"
# Exact spot holdings route observed on the user's Binance spot wallet page.
# Its request has needBtcValuation/quoteAsset but no walletType selector.
SPOT_ASSET_V3_PATH = "/bapi/asset/v3/private/asset-service/asset/get-user-asset"
# These paths are read-only wallet resources. Importing any other request never enables execution.
BALANCE_PATHS = frozenset({
    "/bapi/asset/v1/private/asset-service/wallet/balance",
    "/bapi/asset/v2/private/asset-service/wallet/balance",
    "/bapi/asset/v3/private/asset-service/wallet/balance",
    "/bapi/asset/v1/private/asset-service/wallet/asset",
    "/bapi/asset/v2/private/asset-service/wallet/asset",
    "/bapi/asset/v3/private/asset-service/wallet/asset",
    "/bapi/asset/v1/private/asset-service/asset/get-user-asset",
    SPOT_ASSET_V3_PATH,
})


class JoinUncertain(RequestFailure):
    def __init__(self, message="加入请求的结果尚未确认；先查询加入状态，不会直接重新提交。", *, retry_after=0):
        super().__init__(message, fatal=True, retry_after=retry_after)


class JoinNotSent(RequestFailure):
    """The browser proved its failure occurred before invoking the create request."""


class JoinRejected(RequestFailure):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"币安拒绝加入，业务码 {code}。", fatal=code != "11012017")


def business_data(payload: object) -> object:
    if not isinstance(payload, dict) or payload.get("code") != "000000" or payload.get("success") is False:
        raw = payload.get("code") if isinstance(payload, dict) else None
        code = str(raw) if re.fullmatch(r"[A-Za-z0-9_-]{1,30}", str(raw)) else "未知"
        raise RequestFailure(f"登录或查询失败，业务码 {code}；请更新本机登录参数。", fatal=True)
    return payload.get("data")


@dataclass(frozen=True)
class BalanceQuery:
    url: str
    method: str = "GET"
    body: dict | None = None

    def __post_init__(self):
        official_url(self.url)
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.path not in BALANCE_PATHS or self.method not in {"GET", "POST"}:
            raise ValidationError("余额查询仅支持币安官方 asset-service 的只读 wallet/balance、wallet/asset 或 get-user-asset 请求。")
        if self.body is not None and not isinstance(self.body, dict):
            raise ValidationError("余额请求体必须为 JSON 对象。")
        if self.method == "GET" and self.body:
            raise ValidationError("GET 余额请求不能包含请求体。")
        # Token-bearing URLs and opaque query/body keys are not persisted as configuration.
        allowed_keys = {"asset", "coin", "walletType", "type", "needBtcValuation", "needRpc", "includeEtf", "hideZero", "isPnl", "quoteAsset", "timestamp"}
        params = urllib.parse.parse_qs(parsed.query)
        if set(params) - allowed_keys or (self.body and set(self.body) - allowed_keys):
            raise ValidationError("余额请求包含未识别参数；请使用只读现货资产查询，不包含令牌。")
        for key, value in (self.body or {}).items():
            if not isinstance(value, (str, bool, int, float)) or len(str(value)) > 100:
                raise ValidationError("余额请求参数格式不正确。")
        for key in ("walletType", "type"):
            # Validate every selector: a SPOT body must not hide a conflicting
            # FUTURES query value (or a second value of the same query key).
            selectors = params.get(key, []) + ([self.body[key]] if self.body and key in self.body else [])
            if any(str(selected).upper() not in {"SPOT", "MAIN"} for selected in selectors):
                raise ValidationError("自动跟单仅使用现货 USDT；请导入 SPOT/MAIN 的余额查询。")

    @classmethod
    def from_import(cls, value: ImportedRequest):
        return cls(value.url, value.method, value.body)

    @property
    def explicitly_spot(self) -> bool:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.path == SPOT_ASSET_V3_PATH and self.method == "POST":
            return True
        params = urllib.parse.parse_qs(parsed.query)
        selected = (self.body or {}).get("walletType", (self.body or {}).get("type", params.get("walletType", params.get("type", [None]))[0]))
        return selected is not None and str(selected).upper() in {"SPOT", "MAIN"}


def parse_spot_usdt(payload: object, *, request_is_spot: bool = False) -> Decimal:
    """Fail closed on ambiguous wallet or amount fields. Never use a total valuation as free funds."""
    data = business_data(payload)
    matches = []

    def visit(value, *, spot: bool | None = None, blocked=False):
        if isinstance(value, list):
            for child in value:
                visit(child, spot=spot, blocked=blocked)
        elif isinstance(value, dict):
            for tag in ("walletType", "walletName", "accountType", "type"):
                if tag in value:
                    identified = str(value[tag]).upper() in {"SPOT", "MAIN", "现货"}
                    spot = identified
                    blocked = blocked or not identified
            asset = value.get("asset", value.get("coin"))
            if spot is True and not blocked and asset == "USDT":
                keys = [key for key in ("free", "available", "availableBalance", "freeAmount") if key in value]
                if len(keys) != 1:
                    raise RequestFailure("USDT 可用余额字段缺失或存在歧义；未使用总资产估值。", fatal=True)
                raw = value[keys[0]]
                try:
                    if isinstance(raw, bool):
                        raise ValueError
                    amount = Decimal(str(raw))
                    if not amount.is_finite() or amount < 0:
                        raise ValueError
                except (InvalidOperation, ValueError):
                    raise RequestFailure("现货可用 USDT 余额无效。", fatal=True) from None
                matches.append(amount)
            for key in ("balances", "list", "rows", "assets", "assetBalances", "wallets"):
                if key in value:
                    visit(value[key], spot=spot, blocked=blocked)

    visit(data, spot=True if request_is_spot else None)
    if len(matches) != 1:
        raise RequestFailure("未找到唯一的现货可用 USDT 余额；请在本机导入正确的余额查询请求。", fatal=True)
    return matches[0]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RequestFailure("登录请求发生重定向，已停止；登录参数不会发送到跳转地址。", fatal=True)


@dataclass(frozen=True)
class AccountDetail:
    snapshot: Snapshot
    has_copy: bool
    copy_id: str | None


class PrivateClient:
    """Explicit local credentials, fixed-origin requests, and exactly one POST per join call."""

    def __init__(self, session: Session, balance_query: BalanceQuery, proxy: str = "", *, opener=None):
        self.session = session
        self.balance_query = balance_query
        proxy = validate_proxy(proxy)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {}), NoRedirect())

    def _request(self, url: str, *, method="GET", body=None, joining=False):
        official_url(url)
        headers = dict(self.session.headers)
        headers.update({"Accept": "application/json", "Content-Type": "application/json", "clienttype": "web",
                        "Origin": "https://www.binance.com", "Referer": "https://www.binance.com/zh-CN/copy-trading"})
        encoded = None if body is None else json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=8) as response:
                if response.status != 200 or response.geturl() != url:
                    if joining:
                        raise JoinUncertain(f"创建接口返回 HTTP {int(response.status)} 或非预期地址，结果待核对。")
                    raise RequestFailure("登录接口返回了非预期状态或地址。", fatal=True)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RequestFailure("登录接口响应过大。", fatal=True)
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            try:
                if joining:
                    delay = retry_after_seconds(error.headers.get("Retry-After")) if error.code == 429 else 0
                    raise JoinUncertain(f"创建接口返回 HTTP {error.code}，结果待核对。", retry_after=delay) from None
                if error.code == 429:
                    delay = retry_after_seconds(error.headers.get("Retry-After"))
                    raise RequestFailure("登录查询触发限流。", retry_after=delay, fatal=delay > 3600) from None
                if error.code == 401:
                    raise RequestFailure("币安未接受当前登录（HTTP 401）。请在浏览器刷新现货页，导入一条最新余额请求后重试；旧的登录请求可能已失效。", fatal=True) from None
                raise RequestFailure(f"登录查询 HTTP {error.code}；请检查会话、网络和接口。", fatal=True) from None
            finally:
                error.close()
        except (urllib.error.URLError, OSError, TimeoutError):
            if joining:
                raise JoinUncertain("创建接口超时或连接中断，结果待核对。") from None
            raise RequestFailure("登录查询连接失败，请检查本地代理。") from None
        except (ValueError, UnicodeError, RequestFailure) as error:
            if joining:
                if isinstance(error, JoinUncertain):
                    raise
                raise JoinUncertain("创建接口响应无法解析或校验未通过，结果待核对。") from None
            if isinstance(error, RequestFailure):
                raise
            raise RequestFailure("登录查询响应无法解析；可能需要在官方网页完成验证。", fatal=True) from None

    def balance(self) -> Decimal:
        query = self.balance_query
        return parse_spot_usdt(self._request(query.url, method=query.method, body=query.body), request_is_spot=query.explicitly_spot)

    def detail(self, target: str) -> AccountDetail:
        if not ID_PATTERN.fullmatch(target):
            raise ValidationError("组合 ID 不合法。")
        payload = self._request(DETAIL_ENDPOINT + "?" + urllib.parse.urlencode({"portfolioId": target}))
        snapshot = parse_snapshot(payload, target)
        data = payload["data"]
        has_copy, copy_id = data.get("hasCopy"), data.get("copyPortfolioId")
        if not isinstance(has_copy, bool):
            raise RequestFailure("无法确认本账号是否已加入，停止自动提交。", fatal=True)
        if has_copy and (not isinstance(copy_id, str) or not ID_PATTERN.fullmatch(copy_id)):
            raise RequestFailure("已加入标志存在，但缺少有效跟单组合 ID。", fatal=True)
        if not has_copy and copy_id is not None:
            raise RequestFailure("加入状态与组合 ID 不一致。", fatal=True)
        return AccountDetail(snapshot, has_copy, copy_id)

    def create(self, payload: dict):
        result = self._request("https://www.binance.com" + CREATE_PATH, method="POST", body=payload, joining=True)
        if not isinstance(result, dict):
            raise JoinUncertain("创建接口响应格式异常，结果待核对。")
        if result.get("code") == "000000" and result.get("success") is True:
            return
        raw_code = result.get("code")
        code = str(raw_code) if type(raw_code) in (str, int) and re.fullmatch(r"[0-9]{3,30}", str(raw_code)) else None
        if code == "11012017" and ("success" not in result or result["success"] is False):
            raise JoinRejected(code)
        if result.get("success") is False and code and code != "000000":
            raise JoinRejected(code)
        raise JoinUncertain(f"创建接口未返回明确结果（业务码 {code or '格式异常'}），继续核对。")

from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Callable

from .power import KeepAwake


DETAIL_ENDPOINT = (
    "https://www.binance.com/bapi/futures/v1/friendly/future/"
    "copy-trade/lead-portfolio/detail"
)
ID_PATTERN = re.compile(r"[0-9]{6,30}")
MAX_RESPONSE_BYTES = 1_048_576


class ValidationError(ValueError):
    pass


class RequestFailure(Exception):
    def __init__(self, message: str, *, retry_after: float = 0, fatal: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.fatal = fatal


def portfolio_id(value: str) -> str:
    value = value.strip()
    if ID_PATTERN.fullmatch(value):
        return value
    # A Markdown link can be pasted directly from a chat.
    markdown = re.fullmatch(r"\[[^\]]*\]\((https://[^\s)]+)\)", value)
    if markdown:
        value = markdown.group(1)
    try:
        url = urllib.parse.urlsplit(value)
        valid_host = url.hostname in {"binance.com", "www.binance.com"}
        valid_port = url.port in {None, 443}
    except ValueError:
        raise ValidationError("链接地址不合法。") from None
    if (
        url.scheme != "https" or not valid_host or not valid_port
        or url.username is not None or url.password is not None
    ):
        raise ValidationError("请填写 Portfolio ID，或 www.binance.com 的 HTTPS 详情链接。")
    match = re.fullmatch(
        r"/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?copy-trading/lead-details/([0-9]{6,30})/?",
        url.path,
    )
    if not match:
        raise ValidationError("没有找到带单组合 ID，请粘贴带单员详情链接或纯数字 ID。")
    return match.group(1)


def parse_targets(text: str) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(portfolio_id(line) for line in text.splitlines() if line.strip()))
    if not result:
        raise ValidationError("请至少填写一个带单员详情链接或 Portfolio ID。")
    if len(result) > 20:
        raise ValidationError("最多同时监控 20 个组合。")
    return result


def positive_decimal(value: object) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("金额必须是大于 0 的有限数字。") from None


def validate_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        url = urllib.parse.urlsplit(value)
        if (
            url.scheme not in {"http", "https"} or not url.hostname or not url.port
            or url.username is not None or url.password is not None
            or url.path not in {"", "/"} or url.query or url.fragment
        ):
            raise ValueError
    except ValueError:
        raise ValidationError("代理请使用 http://127.0.0.1:端口 这样的地址，不包含账号密码。") from None
    return value


def detect_local_proxy() -> str:
    """Read an enabled local system proxy without changing network settings."""
    proxies = urllib.request.getproxies()
    for key in ("https", "http"):
        candidate = proxies.get(key, "")
        try:
            candidate = validate_proxy(candidate)
            if candidate and urllib.parse.urlsplit(candidate).hostname in {"localhost", "127.0.0.1", "::1"}:
                return candidate
        except ValidationError:
            continue
    return ""


@dataclass(frozen=True)
class Settings:
    targets: tuple[str, ...]
    interval: float = 2.0
    ceiling: Decimal = Decimal("300")
    proxy: str = ""
    sound: bool = True
    demo: bool = False
    copy_model: str = "FIXED_RATIO"
    keep_awake: bool = False

    def __post_init__(self):
        if not self.targets or len(self.targets) > 20:
            raise ValidationError("请设置 1 到 20 个组合。")
        if any(not ID_PATTERN.fullmatch(value) for value in self.targets):
            raise ValidationError("组合 ID 必须为 6 到 30 位数字。")
        if len(set(self.targets)) != len(self.targets):
            raise ValidationError("组合 ID 不应重复。")
        if isinstance(self.interval, bool) or not math.isfinite(self.interval) or not 0.1 <= self.interval <= 300:
            raise ValidationError("抢位间隔请设为 0.1 到 300 秒；所有组合共用这个频率。")
        object.__setattr__(self, "ceiling", positive_decimal(self.ceiling))
        validate_proxy(self.proxy)
        if self.copy_model not in {"FIXED_RATIO", "FIXED_AMT"}:
            raise ValidationError("跟单模式不正确。")


@dataclass(frozen=True)
class Snapshot:
    portfolio_id: str
    nickname: str
    copiers: int
    capacity: int
    minimum: Decimal | None
    portfolio_status: str
    checked_at: float
    paused: bool | None = False
    portfolio_type: str = "PUBLIC"
    minimum_fixed: Decimal | None = None

    @property
    def slots(self) -> int:
        return max(0, self.capacity - self.copiers)

    def minimum_for(self, copy_model: str = "FIXED_RATIO") -> Decimal | None:
        return self.minimum_fixed if copy_model == "FIXED_AMT" else self.minimum

    def state(self, ceiling: Decimal, copy_model: str = "FIXED_RATIO") -> str:
        if self.portfolio_status != "ACTIVE":
            return "状态待确认" if not self.portfolio_status else "暂不可跟单"
        if self.paused is None or self.portfolio_type not in {"PUBLIC", "PRIVATE"}:
            return "状态待确认"
        if self.paused or self.portfolio_type == "PRIVATE":
            return "暂不可跟单"
        if self.slots <= 0:
            return "名额已满"
        minimum = self.minimum_for(copy_model)
        if minimum is None:
            return "金额未知"
        if minimum > ceiling:
            return "最低金额超限"
        return "发现空位"


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise RequestFailure(f"名额字段 {name} 缺失或格式变化；不推断为空位。", fatal=True)
    return int(str(value))


def parse_snapshot(payload: object, target: str, *, now: float | None = None) -> Snapshot:
    if not isinstance(payload, dict):
        raise RequestFailure("接口返回格式不正确。", fatal=True)
    code = payload.get("code")
    if code != "000000" or payload.get("success") is False:
        safe_code = str(code) if re.fullmatch(r"[A-Za-z0-9_-]{1,30}", str(code)) else "未知"
        raise RequestFailure(f"币安业务返回码 {safe_code}；停止，请先检查接口或访问限制。", fatal=True)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RequestFailure("接口没有返回组合详情。", fatal=True)
    returned_id = data.get("leadPortfolioId")
    if str(returned_id) != target:
        raise RequestFailure("接口返回的组合 ID 不匹配。", fatal=True)
    count = _nonnegative_integer(data.get("currentCopyCount"), "currentCopyCount")
    capacity = _nonnegative_integer(data.get("maxCopyCount"), "maxCopyCount")
    if capacity <= 0:
        raise RequestFailure("名额总数无效；需检查接口字段。", fatal=True)
    # Conservative availability: an effective/risk limit can reduce the public upper bound.
    for field in ("finalEffectiveMaxCopyCount", "riskControlMaxCopyCount"):
        if data.get(field) is not None:
            capacity = min(capacity, _nonnegative_integer(data[field], field))
    minimum = None
    if data.get("fixedRadioMinCopyUsd") is not None:
        try:
            minimum = positive_decimal(data["fixedRadioMinCopyUsd"])
        except ValidationError:
            raise RequestFailure("最低跟单金额字段无效；不发送空位提醒。", fatal=True) from None
    status = data.get("status", "")
    if not isinstance(status, str):
        raise RequestFailure("组合状态字段格式变化。", fatal=True)
    nickname = str(data.get("nickname") or target)
    nickname = "".join(c for c in nickname if c.isprintable())[:60]
    paused = data.get("isPaused")
    if paused is not None and not isinstance(paused, bool):
        raise RequestFailure("暂停状态字段格式变化。", fatal=True)
    portfolio_type = data.get("portfolioType", "")
    if not isinstance(portfolio_type, str):
        raise RequestFailure("组合类型字段格式变化。", fatal=True)
    minimum_fixed = None
    if data.get("fixedAmountMinCopyUsd") is not None:
        try:
            minimum_fixed = positive_decimal(data["fixedAmountMinCopyUsd"])
        except ValidationError:
            raise RequestFailure("定额跟单最低金额字段无效。", fatal=True) from None
    return Snapshot(target, nickname, count, capacity, minimum, status,
                    time.time() if now is None else now, paused, portfolio_type, minimum_fixed)


def retry_after_seconds(value: str | None, *, now: float | None = None) -> float:
    if not value:
        return 60.0
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = parsed.timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            return 60.0
    # Never retry before the server's deadline. Very long cooldowns require a manual restart.
    return max(1.0, seconds)


class PublicClient:
    """Unauthenticated web detail adapter, validated against a live public response on 2026-11-11."""

    def __init__(self, proxy: str = "", *, opener=None):
        proxy = validate_proxy(proxy)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {})
        )

    def fetch(self, target: str) -> Snapshot:
        if not ID_PATTERN.fullmatch(target):
            raise ValidationError("组合 ID 不合法。")
        url = DETAIL_ENDPOINT + "?" + urllib.parse.urlencode({"portfolioId": target})
        request = urllib.request.Request(url, headers={
            "User-Agent": "BinanceSlotMonitor/0.1 (read-only)",
            "Accept": "application/json",
        })
        try:
            with self.opener.open(request, timeout=8) as response:
                final = urllib.parse.urlsplit(response.geturl())
                if final.scheme != "https" or final.hostname != "www.binance.com":
                    raise RequestFailure("接口跳转到了其他域名，已停止。", fatal=True)
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RequestFailure("响应过大，接口可能发生变化。", fatal=True)
            payload = json.loads(body)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 429:
                cooldown = retry_after_seconds(error.headers.get("Retry-After"))
                raise RequestFailure(
                    "触发币安限流，所有组合统一冷却。", retry_after=cooldown,
                    fatal=cooldown > 3600,
                ) from None
            if error.code in {401, 403, 418, 451}:
                raise RequestFailure(f"HTTP {error.code}：访问受限，已停止；请检查网络与官方页面。", fatal=True) from None
            if error.code in {404, 410}:
                raise RequestFailure(f"HTTP {error.code}：公开接口不可用，需要重新验证适配器。", fatal=True) from None
            raise RequestFailure(f"HTTP {error.code}：请求失败。") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise RequestFailure("连接币安失败；请检查网络、DNS 或本地代理。") from None
        except (json.JSONDecodeError, UnicodeError, ValueError):
            raise RequestFailure("响应不是有效 JSON；可能出现访问验证或接口变化。", fatal=True) from None
        return parse_snapshot(payload, target)


class DemoClient:
    """Deterministic synthetic data; never makes a network request."""

    def __init__(self):
        self.counts: dict[str, int] = {}

    def fetch(self, target: str) -> Snapshot:
        turn = self.counts.get(target, 0)
        self.counts[target] = turn + 1
        return Snapshot(target, "演示带单员（模拟）", 1000 if turn < 2 else 999,
                        1000, Decimal("300"), "ACTIVE", time.time())


@dataclass(frozen=True)
class JoinConfirmation:
    operation_id: str
    copy_id: str
    copy_model: str
    amount: Decimal


@dataclass(frozen=True)
class ConfirmationProgress:
    queries: int
    result: str
    next_query_at: float = 0
    query_started_at: float | None = None


@dataclass(frozen=True)
class Event:
    kind: str
    target: str = ""
    snapshot: Snapshot | None = None
    message: str = ""
    retry_after: float = 0
    confirmation: JoinConfirmation | None = None
    progress: ConfirmationProgress | None = None


class Monitor:
    def __init__(self, settings: Settings, emit: Callable[[Event], None], *, client=None,
                 stop: threading.Event | None = None, clock: Callable[[], float] = time.monotonic, joiner=None):
        self.settings = settings
        self.emit = emit
        self.client = client or (DemoClient() if settings.demo else PublicClient(settings.proxy))
        self.stop = stop or threading.Event()
        self.clock = clock
        self.alerted: dict[str, bool] = {}
        self.failures = 0
        self.joiner = joiner
        if self.joiner:
            self.joiner.interval = settings.interval

    def run(self, *, once: bool = False):
        enabled = self.settings.keep_awake and not self.settings.demo
        with KeepAwake(enabled) as power:
            if enabled:
                self.emit(Event("power", message=(
                    "运行期间已请求防止系统自动休眠；屏幕可以熄灭，停止后恢复。" if power.active else
                    "未能启用防休眠，请在 Windows 电源设置中手动关闭自动睡眠。")))
            self._run(once=once)

    def _run(self, *, once: bool = False):
        index = 0
        try:
            if self.joiner:
                while not self.stop.is_set():
                    try:
                        self.joiner.preflight(self.settings.targets, self.stop)
                        self.failures = 0
                        break
                    except RequestFailure as error:
                        self.emit(Event("error" if error.fatal else "notice", message=str(error)))
                        if error.fatal:
                            self.emit(Event("halted", message="启动核验未通过，未发起加入请求。"))
                            return
                        self.failures += 1
                        delay = max(self.settings.interval, error.retry_after, min(60, 2 ** min(self.failures, 6)))
                        self.emit(Event("cooldown", message=f"启动查询暂未完成，{delay:g} 秒后自动重试。", retry_after=delay))
                        self.stop.wait(delay)
                    except Exception:
                        self.emit(Event("error", message="自动加入预检发生内部错误，未发起加入请求。"))
                        self.emit(Event("halted", message="请检查本机配置和记录文件。"))
                        return
            while not self.stop.is_set():
                if self.joiner and set(self.settings.targets) <= self.joiner.completed:
                    self.emit(Event("all_joined", message="全部目标均已确认正在跟单，监控结束。"))
                    break
                if self.joiner and self.joiner.pending:
                    try:
                        delay = self.joiner.recover_pending(self.stop)
                    except RequestFailure as error:
                        self.emit(Event("error", message=str(error)))
                        self.emit(Event("halted", message="结果核对被中断；记录已保留，处理登录或接口问题后可继续。"))
                        break
                    except Exception:
                        self.emit(Event("error", message="结果核对出现内部错误；未确认记录已保留，不会重发创建请求。"))
                        self.emit(Event("halted", message="请重新打开程序继续核对。"))
                        break
                    if self.stop.wait(delay):
                        break
                    continue
                target = self.settings.targets[index]
                if self.joiner and target in self.joiner.completed:
                    index = (index + 1) % len(self.settings.targets)
                    continue
                started = self.clock()
                self.emit(Event("checking", target))
                delay = self.settings.interval
                try:
                    if self.joiner and self.joiner.policy.direct_retry:
                        self.joiner.direct_step(target, self.stop)
                        self.failures = 0
                        index = (index + 1) % len(self.settings.targets)
                        if once and index == 0:
                            break
                        self.stop.wait(max(0, delay - (self.clock() - started)))
                        continue
                    snapshot = self.client.fetch(target)
                    if self.stop.is_set():
                        break
                    self.failures = 0
                    self.emit(Event("snapshot", target, snapshot))
                    available = snapshot.state(self.settings.ceiling, self.settings.copy_model) == "发现空位"
                    if available and not self.alerted.get(target, False):
                        self.emit(Event("available", target, snapshot, "发现空位，尚未提交跟单。"))
                    self.alerted[target] = available
                    if self.joiner:
                        self.joiner.step(snapshot, self.stop)
                except RequestFailure as error:
                    if self.stop.is_set():
                        break
                    self.failures += 1
                    # Retry-After starts when the error was received, not when the request started.
                    started = self.clock()
                    direct = self.joiner is not None and self.joiner.policy.direct_retry
                    backoff = (min(60, .5 * 2 ** min(self.failures - 1, 7)) if direct
                               else min(60, 2 ** min(self.failures, 6)))
                    delay = max(delay, error.retry_after, backoff)
                    if error.fatal:
                        self.emit(Event("error", target, message=str(error)))
                        self.emit(Event("halted", target, message="监控已停止，请检查错误后手动重启。"))
                        break
                    if direct:
                        self.joiner.report_retry(target, delay)
                    else:
                        self.emit(Event("error", target, message=str(error)))
                    # Keep the alert latch across transient errors; stale data never rearms it.
                    if not once:
                        self.emit(Event("cooldown", target,
                                        message="" if direct else f"全部请求暂停 {delay:g} 秒后重试。", retry_after=delay))
                except Exception:
                    self.emit(Event("error", target, message="出现未预期的内部错误；已停止，请检查程序。"))
                    self.emit(Event("halted", target, message="监控已停止，不再发出请求。"))
                    break
                index = (index + 1) % len(self.settings.targets)
                if once and index == 0:
                    break
                if self.stop.wait(max(0, delay - (self.clock() - started))):
                    break
        finally:
            try:
                if self.joiner:
                    self.joiner.close()
            except Exception:
                self.emit(Event("error", message="结束时记录整理未完成；原提交记录保留，请重新打开程序核对。"))
                self.emit(Event("halted", message="已停止，不再发出请求。"))
            finally:
                self.emit(Event("stopped"))

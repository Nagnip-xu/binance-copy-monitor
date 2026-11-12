"""Automatic join policy, durable reservations and reconciliation.

This module does not start itself. The desktop user configures a local account,
investment ceiling and explicitly starts the monitor.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .core import ConfirmationProgress, Event, JoinConfirmation, RequestFailure, Snapshot, ValidationError, positive_decimal
from .private_api import AccountDetail, JoinNotSent, JoinRejected, JoinUncertain


RESERVED_STATES = {"SENDING", "VERIFYING", "UNCERTAIN", "JOINED"}
LOCKED_STATES = RESERVED_STATES | {"EXISTING"}
PENDING_STATES = {"SENDING", "VERIFYING", "UNCERTAIN"}


@dataclass
class PendingConfirmation:
    operation: str
    amount: Decimal
    copy_model: str
    snapshot: Snapshot
    reason: str
    checks: int = 0
    next_check_at: float = 0
    historical: bool = False
    failures: int = 0


def money(value: object) -> Decimal:
    amount = positive_decimal(value)
    if amount.as_tuple().exponent < -8 or amount > Decimal("1000000000"):
        raise ValidationError("金额最多 8 位小数，且不能超过 10 亿 USDT。")
    return amount


@dataclass(frozen=True)
class JoinPolicy:
    total_budget: Decimal | None
    per_target_cap: Decimal
    copy_model: str = "FIXED_RATIO"
    allocation: str = "MINIMUM"
    fixed_amount: Decimal | None = None
    cost_per_order: Decimal | None = None
    margin_mode: str = "FOLLOW_LEAD"
    leverage_mode: str = "FOLLOW_LEAD"
    leverage_value: int | None = None
    total_stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None
    max_position: Decimal | None = None
    direct_retry: bool = False
    max_attempts: int | None = 100

    def __post_init__(self):
        for field in ("total_budget", "per_target_cap"):
            if field == "total_budget" and self.total_budget is None:
                continue
            object.__setattr__(self, field, money(getattr(self, field)))
        if self.copy_model not in {"FIXED_RATIO", "FIXED_AMT"} or self.allocation not in {"MINIMUM", "FIXED"}:
            raise ValidationError("跟单模式或投入方式不正确。")
        if self.margin_mode not in {"FOLLOW_LEAD", "CROSS", "ISOLATED"} or self.leverage_mode not in {"FOLLOW_LEAD", "FIX_LEVERAGE"}:
            raise ValidationError("保证金或杠杆模式不正确。")
        if self.leverage_mode == "FIX_LEVERAGE" and (type(self.leverage_value) is not int or not 1 <= self.leverage_value <= 10):
            raise ValidationError("固定杠杆请填写 1 到 10 的整数。")
        if self.leverage_mode == "FOLLOW_LEAD" and self.leverage_value is not None:
            raise ValidationError("跟随带单员杠杆时不能填写固定杠杆倍数。")
        if self.allocation == "FIXED":
            object.__setattr__(self, "fixed_amount", money(self.fixed_amount))
            if self.fixed_amount > self.per_target_cap or (self.total_budget is not None and self.fixed_amount > self.total_budget):
                raise ValidationError("固定投入金额不能超过单组合上限或累计总预算。")
        if self.copy_model == "FIXED_AMT":
            object.__setattr__(self, "cost_per_order", money(self.cost_per_order))
        if self.max_attempts is not None and (type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10000):
            raise ValidationError("每个组合最多尝试次数请设为 1 到 10000。")
        for name, lower, upper in (("total_stop_loss", 0, 95), ("take_profit", 0, 2000), ("stop_loss", 5, 95), ("max_position", 5, 95)):
            value = getattr(self, name)
            if value is not None:
                if isinstance(value, bool):
                    raise ValidationError("风控比例必须是有效数字。")
                try:
                    value = Decimal(str(value))
                    if not value.is_finite() or not lower <= value <= upper:
                        raise ValueError
                except (ValueError, ArithmeticError):
                    raise ValidationError(f"{name} 必须在 {lower} 到 {upper} 之间。") from None
                object.__setattr__(self, name, value)

    def amount(self, snapshot: Snapshot) -> Decimal | None:
        minimum = snapshot.minimum_for(self.copy_model)
        if minimum is None:
            return None
        minimum = money(minimum)
        amount = self.fixed_amount if self.allocation == "FIXED" else minimum
        if amount < minimum or amount > self.per_target_cap:
            return None
        if self.copy_model == "FIXED_AMT" and self.cost_per_order > amount:
            return None
        return amount

    def payload(self, snapshot: Snapshot) -> dict:
        amount = self.amount(snapshot)
        if amount is None:
            raise ValidationError("最低金额不明或不符合已设投入上限。")
        return {
            "leadPortfolioId": snapshot.portfolio_id, "investAsset": "USDT",
            "investAmount": format(amount, "f"), "copyModel": self.copy_model,
            "costPerOrder": format(self.cost_per_order, "f") if self.copy_model == "FIXED_AMT" else 0,
            "marginMode": self.margin_mode, "leverageMode": self.leverage_mode,
            "leverageValue": self.leverage_value,
            "totalStopLossRate": None if self.total_stop_loss is None else str(self.total_stop_loss),
            "takeProfitRate": None if self.take_profit is None else str(self.take_profit),
            "stopLostRate": None if self.stop_loss is None else str(self.stop_loss),
            "maxPositionPerSymbolRate": None if self.max_position is None else str(self.max_position),
        }


class Journal:
    """One reservation per portfolio across processes; never stores session headers."""

    def __init__(self, path: Path, owner_key: str | None = None):
        self.path = path
        self.owner_key = owner_key
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS joins (
                target TEXT PRIMARY KEY, state TEXT NOT NULL, amount TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, operation_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, copy_id TEXT, updated REAL NOT NULL)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(joins)")}
            if "owner_key" not in columns:
                db.execute("ALTER TABLE joins ADD COLUMN owner_key TEXT NOT NULL DEFAULT ''")
            if "copy_model" not in columns:
                db.execute("ALTER TABLE joins ADD COLUMN copy_model TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, target: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM joins WHERE target=?", (target,)).fetchone()
        return dict(row) if row else None

    def rows(self) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM joins ORDER BY updated DESC")]

    def reserve(self, target: str, amount: Decimal, policy: JoinPolicy, payload: dict) -> str:
        self.assert_owner()
        operation = uuid.uuid4().hex
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT state,attempts FROM joins WHERE target=?", (target,)).fetchone()
            if previous and previous[0] in LOCKED_STATES:
                raise RequestFailure("本地已有成功或未确认的加入记录；先核对，不重复提交。", fatal=True)
            attempts = previous[1] if previous else 0
            if policy.max_attempts is not None and attempts >= policy.max_attempts:
                raise RequestFailure("已达到此组合的累计尝试次数上限。", fatal=True)
            used = sum((Decimal(value) for state, value in db.execute("SELECT state,amount FROM joins") if state in RESERVED_STATES), Decimal(0))
            if policy.total_budget is not None and used + amount > policy.total_budget:
                raise RequestFailure("已达到累计总投入上限（包含历史成功和未确认提交）。", fatal=True)
            db.execute("""INSERT INTO joins (target,state,amount,attempts,operation_id,payload_hash,copy_id,updated,owner_key,copy_model)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(target) DO UPDATE SET state=excluded.state,amount=excluded.amount,
                attempts=excluded.attempts,operation_id=excluded.operation_id,
                payload_hash=excluded.payload_hash,copy_id=NULL,updated=excluded.updated,owner_key=excluded.owner_key,copy_model=excluded.copy_model""",
                (target, "SENDING", str(amount), attempts + 1, operation, digest, None, time.time(), self.owner_key, policy.copy_model))
        return operation

    def transition(self, target: str, state: str, *, copy_id: str | None = None, operation: str | None = None):
        self.assert_owner(target)
        if state not in {"SENDING", "VERIFYING", "UNCERTAIN", "JOINED", "EXISTING", "REJECTED", "CANCELLED"}:
            raise ValueError("unknown journal state")
        with self._connect() as db:
            if operation is None:
                result = db.execute("UPDATE joins SET state=?,copy_id=?,updated=? WHERE target=? AND owner_key=?", (state, copy_id, time.time(), target, self.owner_key))
            else:
                result = db.execute("UPDATE joins SET state=?,copy_id=?,updated=? WHERE target=? AND operation_id=? AND owner_key=?",
                                    (state, copy_id, time.time(), target, operation, self.owner_key))
            if result.rowcount != 1:
                raise RequestFailure("加入记录已变化；停止处理旧请求，请核对本机记录。", fatal=True)

    def cancel_before_send(self, target: str, operation: str):
        """Release only a proven unsent create operation; never use for ambiguous POSTs."""
        self.assert_owner(target)
        with self._connect() as db:
            result = db.execute("UPDATE joins SET state='CANCELLED',attempts=attempts-1,updated=? "
                                "WHERE target=? AND operation_id=? AND owner_key=? AND state='SENDING' AND attempts>0",
                                (time.time(), target, operation, self.owner_key))
            if result.rowcount != 1:
                raise RequestFailure("未发送记录已变化，停止自动恢复。", fatal=True)

    def exclusive(self):
        return RunLock(self.path.with_suffix(".runlock"))

    def assert_owner(self, target: str | None = None):
        if not self.owner_key:
            raise RequestFailure("未绑定本机登录会话，不能修改加入记录。", fatal=True)
        row = self.get(target) if target else None
        if row and row["owner_key"] != self.owner_key:
            raise RequestFailure("此记录属于其他或旧的登录会话；禁止用当前会话确认或解锁。请使用创建记录时的会话。", fatal=True)

    def existing(self, target: str, copy_id: str):
        self.assert_owner(target)
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO joins (target,state,amount,attempts,operation_id,payload_hash,copy_id,updated,owner_key) VALUES (?,?,?,?,?,?,?,?,?)",
                       (target, "EXISTING", "0", 0, uuid.uuid4().hex, "", copy_id, time.time(), self.owner_key))

    def release_after_manual_check(self, target: str, *, operation: str | None = None):
        """Explicit user action only; caller must first verify hasCopy=false in Binance."""
        self.assert_owner(target)
        with self._connect() as db:
            result = db.execute("UPDATE joins SET state='CANCELLED',updated=? WHERE target=? AND owner_key=? "
                                "AND state IN ('SENDING','VERIFYING','UNCERTAIN') AND (? IS NULL OR operation_id=?)",
                                (time.time(), target, self.owner_key, operation, operation))
            if result.rowcount != 1:
                raise RequestFailure("未确认记录已变化，未解除；请重新核对。", fatal=True)


class AutoJoiner:
    def __init__(self, client, policy: JoinPolicy, journal: Journal, emit: Callable[[Event], None]):
        self.client, self.policy, self.journal, self.emit = client, policy, journal, emit
        self.completed: set[str] = set()
        self.ready = False
        self.cached_balance = Decimal(0)
        self.last_skip: dict[str, str] = {}
        self.run_lock = None
        self.startup_snapshots: dict[str, Snapshot] = {}
        self.attempt_counts: dict[str, int] = {}
        self.progress_at: dict[str, float] = {}
        self.pending_progress: set[str] = set()
        self.latest_rejections: dict[str, str] = {}
        self.retry_counts: dict[str, int] = {}
        self.retry_delays: dict[str, float] = {}
        self.retrying: set[str] = set()
        self.pending: dict[str, PendingConfirmation] = {}
        self.balance_needs_refresh = False
        self.confirmation_not_before = 0.0
        self.interval = 0.1  # Monitor supplies the user's validated shared interval.

    def _balance(self):
        self.cached_balance = self.client.balance()
        self.emit(Event("balance", message=str(self.cached_balance)))

    def preflight(self, targets: tuple[str, ...], stop: threading.Event):
        if self.run_lock is None:
            self.run_lock = self.journal.exclusive()
            self.run_lock.acquire()
        self._balance()  # Success is required: guest detail data cannot prove authentication.
        for target in targets:
            if stop.is_set():
                return
            if target in self.completed:
                continue
            detail = self.client.detail(target)
            self.emit(Event("target_detail", target, detail.snapshot, "启动"))
            record = self.journal.get(target)
            self.attempt_counts[target] = record["attempts"] if record else 0
            self.startup_snapshots[target] = detail.snapshot
            if record and record["state"] in LOCKED_STATES:
                self.journal.assert_owner(target)
            if detail.has_copy:
                if record and record["state"] in PENDING_STATES:
                    self._confirmed(target, detail, self._pending_record(record, detail.snapshot), deduct=False)
                    continue
                elif record and record["state"] in RESERVED_STATES:
                    self.journal.transition(target, "JOINED", copy_id=detail.copy_id, operation=record["operation_id"])
                else:
                    self.journal.existing(target, detail.copy_id)
                self.completed.add(target)
                self.emit(Event("joined_existing", target, detail.snapshot, "官方状态已显示正在跟单，跳过重复加入。"))
            elif record and record["state"] in PENDING_STATES:
                if target not in self.pending:
                    pending = self._pending_record(record, detail.snapshot)
                    self._begin_confirmation(target, pending)
                    self._waiting(target, pending, "官方尚未显示已跟单", self.interval)
                else:
                    pending = self.pending[target]
                    pending.checks += 1
                    self._waiting(target, pending, "官方尚未显示已跟单", self.interval)
            elif record and record["state"] in LOCKED_STATES:
                raise RequestFailure(f"{target} 存在历史成功或未确认记录，与当前页面状态需人工核对；已阻止重发。", fatal=True)
            elif self.policy.direct_retry:
                if detail.snapshot.state(self.policy.per_target_cap, self.policy.copy_model) not in {"发现空位", "名额已满"} or self.policy.amount(detail.snapshot) is None:
                    raise RequestFailure(f"{target} 启动核验未通过：组合状态或最低金额不符合安全上限；未提交。", fatal=True)
                if self.policy.amount(detail.snapshot) > self.cached_balance:
                    raise RequestFailure("可用现货 USDT 不足，未开始抢位。", fatal=True)
        self.ready = True
        self.emit(Event("preflight_ok", message="登录、余额与历史加入记录检查完成。"))

    def _pending_record(self, record: dict, snapshot: Snapshot) -> PendingConfirmation:
        return PendingConfirmation(record["operation_id"], money(record["amount"]), record.get("copy_model", ""),
                                   snapshot, "恢复上次未确认的提交记录。", checks=1, historical=True)

    def _begin_confirmation(self, target: str, pending: PendingConfirmation):
        self.journal.transition(target, "VERIFYING", operation=pending.operation)
        self.pending[target] = pending
        self.pending_progress.discard(target)
        self.retrying.discard(target)
        self.emit(Event("verifying", target, pending.snapshot,
                        f"抢位已暂停。{pending.reason} 当前仅核对上次提交，不发送新的抢位请求；正常核对间隔 {self.interval:g} 秒。"))

    def _waiting(self, target: str, pending: PendingConfirmation, reason: str, delay: float):
        pending.reason = reason
        now = time.monotonic()
        pending.next_check_at = max(now + delay, self.confirmation_not_before)
        self.confirmation_not_before = pending.next_check_at
        delay = pending.next_check_at - now
        # All targets share one serial cadence, including server-directed cooldowns.
        for other_target, other in self.pending.items():
            if other_target != target and other.next_check_at < self.confirmation_not_before:
                other.next_check_at = self.confirmation_not_before
                self.emit(Event("confirmation_schedule", other_target, other.snapshot,
                                progress=ConfirmationProgress(other.checks, other.reason, other.next_check_at)))
        self.emit(Event("confirmation_waiting", target, pending.snapshot,
                        f"{pending.snapshot.nickname}({target}) 结果核对中 | 本轮已查询 {pending.checks} 次 | "
                        f"{reason} | {delay:.3g} 秒后再查（抢位已暂停）。", retry_after=delay,
                        progress=ConfirmationProgress(pending.checks, reason, pending.next_check_at)))

    def _confirmed(self, target: str, detail: AccountDetail, pending: PendingConfirmation, *, deduct=True):
        self.journal.transition(target, "JOINED", copy_id=detail.copy_id, operation=pending.operation)
        self.completed.add(target)
        self.pending.pop(target, None)
        if deduct and pending.historical:
            # Startup funds may already reflect an older pending allocation.
            # Read actual available funds before any subsequent new creation.
            self.balance_needs_refresh = True
        elif deduct:
            self.cached_balance -= pending.amount
        self.pending_progress.discard(target)
        self.retrying.discard(target)
        self.emit(Event("joined", target, detail.snapshot,
                        f"二次确认加入成功；跟单组合 {detail.copy_id}，投入 {pending.amount} USDT。",
                        confirmation=JoinConfirmation(pending.operation, detail.copy_id, pending.copy_model, pending.amount)))

    def _check_confirmation(self, target: str) -> float:
        pending = self.pending[target]
        self.emit(Event("confirmation_checking", target, pending.snapshot,
                        progress=ConfirmationProgress(pending.checks, pending.reason,
                                                      query_started_at=time.monotonic())))
        pending.checks += 1
        delay = self.interval
        try:
            detail = self.client.detail(target)
        except RequestFailure as error:
            if error.fatal:
                self.journal.transition(target, "UNCERTAIN", operation=pending.operation)
                pending.reason = str(error)
                raise RequestFailure(f"跟单结果查询被中断：{error} 未确认记录保留；重新登录后可继续核对。", fatal=True) from None
            pending.failures += 1
            backoff = min(60, .5 * 2 ** min(pending.failures - 1, 7))
            delay = max(delay, error.retry_after, backoff)
            self._waiting(target, pending, f"查询暂未完成：{error}", delay)
            return delay
        if detail.has_copy:
            self._confirmed(target, detail, pending)
            return 0
        pending.snapshot = detail.snapshot
        self.emit(Event("target_detail", target, detail.snapshot))
        pending.failures = 0
        self._waiting(target, pending, "官方尚未显示已跟单", delay)
        return delay

    def recover_pending(self, stop: threading.Event) -> float:
        # Pause all creates until the outstanding outcome is resolved. Checking
        # negative snapshots or unchanged funds is never treated as cancellation.
        if stop.is_set() or not self.pending:
            return 0
        target = min(self.pending, key=lambda key: (self.pending[key].next_check_at, self.pending[key].checks))
        remaining = max(self.pending[target].next_check_at, self.confirmation_not_before) - time.monotonic()
        if remaining > 0:
            return remaining
        self._check_confirmation(target)
        next_check = min((value.next_check_at for value in self.pending.values()), default=time.monotonic())
        return max(0, max(next_check, self.confirmation_not_before) - time.monotonic())

    def direct_step(self, target: str, stop: threading.Event):
        self.step(self.startup_snapshots[target], stop, startup_checked=True)

    def report_retry(self, target: str, delay: float):
        self.retry_counts[target] = self.retry_counts.get(target, 0) + 1
        self.retry_delays[target] = delay
        self.retrying.add(target)
        self._progress(self.startup_snapshots[target])

    def _progress(self, snapshot: Snapshot, *, force=False, stopped=False):
        target = snapshot.portfolio_id
        now = time.monotonic()
        self.pending_progress.add(target)
        if force or target not in self.progress_at or now - self.progress_at[target] >= 30:
            mode = "直接抢位中" if self.policy.direct_retry else "等待空位中"
            if target in self.retrying:
                mode = f"网络恢复中（{self.retry_delays[target]:g} 秒后重试）"
            if stopped:
                mode = "已停止"
            transient = (f" | 查询重试 {self.retry_counts[target]} 次（自动恢复）"
                         if self.retry_counts.get(target) else "")
            self.emit(Event("join_summary" if stopped else "join_progress", target, snapshot,
                            f"{snapshot.nickname}({target}) {mode} | 已提交 {self.attempt_counts.get(target, 0)} 次 | "
                            f"最近拒绝：{self.latest_rejections.get(target, '—')}{transient}"))
            self.progress_at[target] = now
            self.pending_progress.discard(target)

    def step(self, snapshot: Snapshot, stop: threading.Event, *, startup_checked=False):
        target = snapshot.portfolio_id
        if not self.ready:
            raise RequestFailure("自动加入尚未通过登录与余额预检。", fatal=True)
        if target in self.completed or stop.is_set():
            return
        if self.pending:
            return  # A pending outcome globally blocks further money movement.
        if self.balance_needs_refresh:
            self._balance()
            self.balance_needs_refresh = False
        state = snapshot.state(self.policy.per_target_cap, self.policy.copy_model)
        if state != "发现空位" and not (self.policy.direct_retry and state == "名额已满"):
            if self.last_skip.get(target) != state:
                self.emit(Event("join_skipped", target, snapshot, f"未提交：{state}。"))
                self.last_skip[target] = state
            return
        cached_direct = startup_checked and self.policy.direct_retry and self.startup_snapshots.get(target) is snapshot
        if time.time() - snapshot.checked_at > 10 and not cached_direct:
            raise RequestFailure("名额数据已过期，停止自动加入。", fatal=True)
        # Normal mode rechecks account state and funds immediately before submission.
        # Direct mode uses the startup balance minus confirmed allocations, matching its explicit UI description.
        if not self.policy.direct_retry:
            detail = self.client.detail(target)
            self.emit(Event("target_detail", target, detail.snapshot))
            if detail.has_copy:
                self.journal.existing(target, detail.copy_id)
                self.completed.add(target)
                self.emit(Event("joined_existing", target, detail.snapshot, "本账号已加入，跳过提交。"))
                return
            snapshot = detail.snapshot
            if snapshot.state(self.policy.per_target_cap, self.policy.copy_model) != "发现空位":
                return
            self._balance()
        amount = self.policy.amount(snapshot)
        if amount is None:
            self.emit(Event("join_skipped", target, snapshot, "最低金额不符合投入设置。"))
            return
        if amount > self.cached_balance:
            raise RequestFailure("可用现货 USDT 不足，自动加入已停止。", fatal=True)
        if time.time() - snapshot.checked_at > 10 and not cached_direct:
            raise RequestFailure("余额查询后名额数据已过期，未提交加入。", fatal=True)
        if stop.is_set():
            return
        payload = self.policy.payload(snapshot)
        operation = self.journal.reserve(target, amount, self.policy, payload)
        self.attempt_counts[target] = self.attempt_counts.get(target, 0) + 1
        if stop.is_set():
            self.journal.cancel_before_send(target, operation)
            self.attempt_counts[target] -= 1
            return
        self.emit(Event("joining", target, snapshot, f"已记录提交 {operation[:8]}，投入 {amount} USDT。"))
        reason = "币安创建接口已返回受理，等待账户状态更新。"
        confirmation_delay = 0
        try:
            self.client.create(payload)
        except JoinNotSent:
            self.journal.cancel_before_send(target, operation)
            self.attempt_counts[target] -= 1
            raise
        except JoinRejected as error:
            self.journal.transition(target, "REJECTED", operation=operation)
            if error.code == "11012017":
                self.retrying.discard(target)
                self.latest_rejections[target] = "币安业务错误 11012017：带单员的项目已满。"
                self.emit(Event("join_full", target, snapshot))
                self._progress(snapshot)
                return
            raise
        except (JoinUncertain, RequestFailure) as error:
            # The POST may have reached the server. No new POST follows this branch.
            reason = f"创建响应未能确认：{error}"
            confirmation_delay = error.retry_after
        except Exception:
            reason = "创建响应处理出现内部错误，原请求结果尚未确认。"
        self._begin_confirmation(target, PendingConfirmation(operation, amount, self.policy.copy_model, snapshot, reason))
        if confirmation_delay > 0:
            self.confirmation_not_before = time.monotonic() + confirmation_delay
            self._waiting(target, self.pending[target], reason, confirmation_delay)
            return
        # Even if Stop was pressed in flight, try this first read-only check.
        self._check_confirmation(target)

    def reconcile(self, target: str) -> AccountDetail:
        self.journal.assert_owner(target)
        record = self.journal.get(target)
        detail = self.client.detail(target)
        if detail.has_copy and record:
            self.journal.transition(target, "JOINED", copy_id=detail.copy_id, operation=record["operation_id"])
            self.completed.add(target)
        return detail

    def close(self):
        try:
            for target, pending in tuple(self.pending.items()):
                self.journal.transition(target, "UNCERTAIN", operation=pending.operation)
                self.emit(Event("confirmation_paused", target, pending.snapshot,
                                f"{target} 结果核对已暂停；未确认记录保留。下次开始将接着查询，不重复提交。"))
                self.pending_progress.discard(target)
            self.pending.clear()
            for target in tuple(self.pending_progress):
                snapshot = self.startup_snapshots.get(target)
                if snapshot is not None:
                    self._progress(snapshot, force=True, stopped=True)
        finally:
            if self.run_lock:
                self.run_lock.release()
                self.run_lock = None


def pending_records(path: Path) -> list[dict]:
    """Inspect old and new ledgers without creating or migrating a database."""
    if not path.exists():
        return []
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM joins WHERE state IN ('SENDING','VERIFYING','UNCERTAIN') ORDER BY updated DESC")]
        finally:
            db.close()
    except sqlite3.Error:
        raise RequestFailure("无法读取未确认记录，请检查本机记录文件是否可用。", fatal=True) from None


def review_pending(client, journal: Journal, target: str, operation: str, emit,
                   *, manual_release: bool = False):
    """Read-only reconciliation, or a separately confirmed manual release; never creates."""
    with journal.exclusive():
        journal.assert_owner(target)
        record = journal.get(target)
        if not record or record["operation_id"] != operation or record["state"] not in PENDING_STATES:
            raise RequestFailure("未确认记录已变化，请关闭核对窗口后重新打开。", fatal=True)
        detail = client.detail(target)  # Transport verifies the account before this read.
        if detail.has_copy:
            journal.transition(target, "JOINED", copy_id=detail.copy_id, operation=operation)
            emit(Event("joined", target, detail.snapshot, "官方已确认正在跟单。",
                       confirmation=JoinConfirmation(operation, detail.copy_id,
                                                     record.get("copy_model", ""), money(record["amount"]))))
        elif manual_release:
            journal.release_after_manual_check(target, operation=operation)
            emit(Event("records_checked", target, detail.snapshot, message=f"{target}：经你确认未创建且无扣款，并重新查询官方状态后，已解除未确认锁定。未发送创建请求；需要抢位时请点击“开始监控”。"))
        else:
            emit(Event("records_checked", target, detail.snapshot, message=f"{target}：官方当前尚未显示已跟单，未确认记录继续保留。点击“开始监控”可持续查询；此结果不能单独证明原请求已失败。"))


class RunLock:
    """Kernel-owned file lock shared by automatic submission and manual recovery."""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RequestFailure("另一个程序正在自动加入或核对记录；请先停止它。", fatal=True) from None

    def release(self):
        if self.file is not None:
            self.file.close()  # OS releases the lock, including after a process crash.
            self.file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class DemoTradeClient:
    """Full automatic-join demonstration with no HTTP transport or real funds."""

    def __init__(self):
        self.joined: dict[str, str] = {}

    def balance(self):
        return Decimal("100000")

    def detail(self, target: str):
        snapshot = Snapshot(target, "演示带单员（模拟）", 999, 1000, Decimal("300"), "ACTIVE", time.time(), minimum_fixed=Decimal("300"))
        return AccountDetail(snapshot, target in self.joined, self.joined.get(target))

    def create(self, payload):
        target = payload["leadPortfolioId"]
        self.joined[target] = str(9000000000000000000 + len(self.joined))

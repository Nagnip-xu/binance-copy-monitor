"""Uncertain financial outcomes stay read-only until confirmed or manually reviewed."""
import sqlite3
import tempfile
import threading
import tkinter as tk
import unittest
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from binance_monitor.core import ConfirmationProgress, Event, Monitor, RequestFailure, Settings
from binance_monitor.gui import App
from binance_monitor.private_api import AccountDetail, JoinRejected, JoinUncertain
from binance_monitor.success_view import success_text
from binance_monitor.trading import AutoJoiner, Journal, JoinPolicy, pending_records, review_pending
from tests.test_notifications_power import destroy_root
from tests.test_trading import FakeClient, TARGET, OTHER, COPY_ID, snapshot


class VirtualStop(threading.Event):
    def __init__(self, clock, limit=20):
        super().__init__()
        self.clock, self.limit, self.waits = clock, limit, []

    def wait(self, delay):
        self.waits.append(delay)
        self.clock[0] += delay
        if len(self.waits) >= self.limit:
            self.set()
        return self.is_set()


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "joins.sqlite3"
        self.journal = Journal(self.path, "test-owner")
        self.client, self.events, self.clock = FakeClient(), [], [100.0]
        self.policy = JoinPolicy(None, "400", direct_retry=True, max_attempts=None)
        self.joiner = AutoJoiner(self.client, self.policy, self.journal, self.events.append)
        self.addCleanup(self.joiner.close)
        self.addCleanup(patch.stopall)
        patch("binance_monitor.trading.time.monotonic", side_effect=lambda: self.clock[0]).start()

    def reserve(self, target=TARGET, policy=None):
        return self.journal.reserve(target, Decimal("300"), policy or self.policy, {})

    def run_monitor(self, limit=20, interval=.1):
        stop = VirtualStop(self.clock, limit)
        public = Mock()
        public.fetch.side_effect = AssertionError("must not poll public slots")
        Monitor(Settings((TARGET,), interval=interval), self.events.append, joiner=self.joiner,
                client=public, stop=stop, clock=lambda: self.clock[0]).run()
        return stop

    def due_check(self):
        self.clock[0] = max(value.next_check_at for value in self.joiner.pending.values())
        return self.joiner.recover_pending(threading.Event())


class RecoveryTests(RecoveryFixture):
    def test_normal_confirmation_uses_user_interval_without_growing_after_many_queries(self):
        self.reserve()
        stop = self.run_monitor(limit=50, interval=.1)
        self.assertTrue(stop.is_set())
        self.assertEqual(len(stop.waits), 50)
        self.assertTrue(all(abs(delay - .1) < 1e-8 for delay in stop.waits))
        self.assertFalse(self.client.creates)
        self.assertEqual(self.journal.get(TARGET)["state"], "UNCERTAIN")

    def test_nondefault_interval_reaches_recovered_submission(self):
        self.reserve()
        stop = self.run_monitor(limit=5, interval=2.5)
        self.assertEqual(self.joiner.interval, 2.5)
        self.assertEqual(stop.waits, [2.5] * 5)
        self.assertFalse(self.client.creates)

    def test_confirmation_error_backoff_resets_after_valid_negative_response(self):
        self.reserve()
        self.joiner.preflight((TARGET,), threading.Event())
        self.client.detail = Mock(side_effect=[RequestFailure("synthetic timeout"), RequestFailure("synthetic timeout"),
                                              AccountDetail(snapshot(), False, None), RequestFailure("synthetic timeout")])
        for expected in (.5, 1, .1, .5):
            self.assertAlmostEqual(self.due_check(), expected)
        self.assertFalse(self.client.creates)
        recent = [event for event in self.events if event.kind == "confirmation_waiting"]
        self.assertIn("synthetic timeout", recent[-1].message)

    def test_multiple_pending_queries_share_interval_and_take_turns(self):
        for target in (TARGET, OTHER):
            self.reserve(target)
        self.joiner.preflight((TARGET, OTHER), threading.Event())
        reads = []
        def detail(target):
            reads.append((target, self.clock[0]))
            return AccountDetail(snapshot(portfolio_id=target), False, None)
        self.client.detail = detail
        for _ in range(6):
            self.due_check()
        self.assertEqual([target for target, _ in reads], [TARGET, OTHER] * 3)
        self.assertTrue(all(abs(right[1] - left[1] - .1) < 1e-8 for left, right in zip(reads, reads[1:])))
        self.assertFalse(self.client.creates)

    def test_each_confirmation_result_is_logged_without_waiting_thirty_seconds(self):
        self.reserve()
        self.joiner.preflight((TARGET,), threading.Event())
        initial = next(event for event in self.events if event.kind == "confirmation_waiting")
        self.assertEqual(initial.progress.queries, 1)  # Includes the startup read of the old request.
        self.assertIn("本轮已查询 1 次", initial.message)
        self.client.detail = Mock(wraps=self.client.detail)
        for _ in range(3):
            self.due_check()
        waiting = [event for event in self.events if event.kind == "confirmation_waiting"]
        self.assertEqual([event.progress.queries for event in waiting], [1, 2, 3, 4])
        self.assertEqual(self.client.detail.call_count, 3)
        self.assertFalse(self.client.creates)
        self.assertTrue(all(event.progress.result == "官方尚未显示已跟单" for event in waiting))
        self.assertEqual(sum(event.kind == "confirmation_checking" for event in self.events), 3)

    def test_accepted_response_waits_for_account_confirmation(self):
        self.joiner.preflight((TARGET,), threading.Event())
        self.client.create = Mock()  # Accepted by API, not reflected in account detail yet.
        self.joiner.direct_step(TARGET, threading.Event())
        for _ in range(5):
            self.due_check()
            self.joiner.direct_step(TARGET, threading.Event())
        self.client.create.assert_called_once()
        self.assertEqual(self.journal.get(TARGET)["state"], "VERIFYING")
        self.assertFalse(any(e.kind == "joined" for e in self.events))
        self.client.joined = True
        self.due_check()
        self.assertEqual(sum(e.kind == "joined" for e in self.events), 1)

    def test_delayed_confirmation_beyond_three_reads_keeps_running_with_one_post(self):
        self.client.result = JoinUncertain("synthetic POST timeout")
        reads = []
        def detail(target):
            reads.append(target)
            joined = len(reads) >= 7
            return AccountDetail(snapshot(), joined, COPY_ID if joined else None)
        self.client.detail = detail
        stop = self.run_monitor()
        self.assertFalse(stop.is_set())
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertEqual(sum(e.kind == "joined" for e in self.events), 1)
        self.assertNotIn("halted", [e.kind for e in self.events])
        self.assertTrue(stop.waits)
        self.assertTrue(all(delay <= .100001 for delay in stop.waits))

    def test_read_timeouts_and_retry_after_never_resubmit_or_query_too_early(self):
        self.joiner.preflight((TARGET,), threading.Event())
        self.client.result = JoinUncertain("synthetic POST timeout")
        self.client.detail = Mock(side_effect=RequestFailure("synthetic read rate limit", retry_after=90))
        self.joiner.direct_step(TARGET, threading.Event())
        self.assertEqual(self.joiner.recover_pending(threading.Event()), 90)
        self.client.detail.assert_called_once()
        self.clock[0] += 89
        self.assertEqual(self.joiner.recover_pending(threading.Event()), 1)
        self.client.detail.assert_called_once()
        self.client.detail.side_effect = None
        self.client.detail.return_value = AccountDetail(snapshot(), True, COPY_ID)
        self.clock[0] += 1
        self.joiner.recover_pending(threading.Event())
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertEqual(len(self.client.creates), 1)

    def test_create_rate_limit_is_respected_before_first_confirmation_read(self):
        self.joiner.preflight((TARGET,), threading.Event())
        self.client.result = JoinUncertain("HTTP 429", retry_after=90)
        self.client.detail = Mock(wraps=self.client.detail)
        self.joiner.direct_step(TARGET, threading.Event())
        self.client.detail.assert_not_called()
        self.assertEqual(self.joiner.recover_pending(threading.Event()), 90)
        self.assertEqual(len(self.client.creates), 1)

    def test_stop_preserves_same_operation_and_restart_resumes_without_new_post(self):
        self.client.result = JoinUncertain()
        stop = self.run_monitor(limit=4)
        self.assertTrue(stop.is_set())
        record = self.journal.get(TARGET)
        self.assertEqual(record["state"], "UNCERTAIN")
        self.client.joined = True
        restarted = AutoJoiner(self.client, self.policy, self.journal, self.events.append)
        try:
            restarted.preflight((TARGET,), threading.Event())
            restarted.direct_step(TARGET, threading.Event())
        finally:
            restarted.close()
        self.assertEqual(self.journal.get(TARGET)["operation_id"], record["operation_id"])
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertEqual(len(self.client.creates), 1)

    def test_restart_receipt_uses_historical_amount_and_model(self):
        old_policy = JoinPolicy(None, "400", copy_model="FIXED_AMT", cost_per_order="10")
        op = self.journal.reserve(TARGET, Decimal("350.12345678"), old_policy, {})
        self.client.joined = True
        self.joiner.preflight((TARGET,), threading.Event())
        receipt = next(e.confirmation for e in self.events if e.kind == "joined")
        self.assertEqual(receipt.operation_id, op)
        self.assertEqual(receipt.amount, Decimal("350.12345678"))
        self.assertEqual(receipt.copy_model, "FIXED_AMT")
        self.assertEqual(self.joiner.cached_balance, Decimal("600"))

    def test_historical_confirmation_refreshes_funds_before_another_target(self):
        self.reserve()
        self.client.balance = Mock(return_value=Decimal("600"))
        self.joiner.preflight((TARGET, OTHER), threading.Event())
        self.client.joined = True
        self.due_check()
        self.assertTrue(self.joiner.balance_needs_refresh)
        self.client.balance.return_value = Decimal("299")
        with self.assertRaises(RequestFailure):
            self.joiner.direct_step(OTHER, threading.Event())
        self.assertEqual(self.client.balance.call_count, 2)
        self.assertFalse(self.client.creates)

    def test_wrong_account_and_historical_success_remain_blocked(self):
        op = self.reserve()
        wrong = AutoJoiner(self.client, self.policy, Journal(self.path, "wrong-owner"), self.events.append)
        try:
            with self.assertRaises(RequestFailure):
                wrong.preflight((TARGET,), threading.Event())
        finally:
            wrong.close()
        self.journal.transition(TARGET, "JOINED", copy_id=COPY_ID, operation=op)
        with self.assertRaises(RequestFailure):
            self.joiner.preflight((TARGET,), threading.Event())
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertFalse(self.client.creates)

    def test_login_failure_during_confirmation_retains_record_and_reason(self):
        self.reserve()
        self.joiner.preflight((TARGET,), threading.Event())
        self.client.detail = Mock(side_effect=RequestFailure("synthetic account changed", fatal=True))
        with self.assertRaisesRegex(RequestFailure, "synthetic account changed"):
            self.due_check()
        self.joiner.close()
        self.assertEqual(self.journal.get(TARGET)["state"], "UNCERTAIN")
        self.assertFalse(self.client.creates)

    def test_internal_recovery_error_is_reported_and_stops_cleanly(self):
        self.reserve()
        self.joiner.recover_pending = Mock(side_effect=RuntimeError("synthetic private error"))
        self.run_monitor()
        kinds = [e.kind for e in self.events]
        self.assertIn("error", kinds)
        self.assertIn("halted", kinds)
        self.assertEqual(kinds[-1], "stopped")
        self.assertFalse(self.client.creates)
        self.assertEqual(self.journal.get(TARGET)["state"], "UNCERTAIN")
        self.assertNotIn("synthetic private error", " ".join(e.message for e in self.events))

    def test_preflight_retry_does_not_duplicate_success_notification(self):
        self.reserve()
        calls = []
        def detail(target):
            calls.append(target)
            if target == OTHER and calls.count(OTHER) == 1:
                raise RequestFailure("synthetic temporary query error")
            return AccountDetail(snapshot(portfolio_id=target), True, COPY_ID)
        self.client.detail = detail
        with self.assertRaises(RequestFailure):
            self.joiner.preflight((TARGET, OTHER), threading.Event())
        self.joiner.preflight((TARGET, OTHER), threading.Event())
        self.assertEqual(calls.count(TARGET), 1)
        self.assertEqual(sum(e.kind == "joined" for e in self.events), 1)

    def test_multiple_pending_targets_are_all_queried_and_block_new_posts(self):
        for target in (TARGET, OTHER):
            self.reserve(target)
        self.joiner.preflight((TARGET, OTHER), threading.Event())
        self.client.detail = Mock(wraps=self.client.detail)
        for _ in range(4):
            self.clock[0] = min(p.next_check_at for p in self.joiner.pending.values())
            self.joiner.recover_pending(threading.Event())
            self.joiner.direct_step(TARGET, threading.Event())
        self.assertEqual(set(call.args[0] for call in self.client.detail.call_args_list), {TARGET, OTHER})
        self.assertFalse(self.client.creates)

    def test_confirmation_rate_limit_cools_all_pending_targets(self):
        for target in (TARGET, OTHER):
            self.reserve(target)
        self.joiner.preflight((TARGET, OTHER), threading.Event())
        self.clock[0] += 2
        self.client.detail = Mock(side_effect=RequestFailure("synthetic global 429", retry_after=90))
        self.assertEqual(self.joiner.recover_pending(threading.Event()), 90)
        self.clock[0] += 89
        self.assertEqual(self.joiner.recover_pending(threading.Event()), 1)
        self.client.detail.assert_called_once()
        schedule = next(event for event in self.events if event.kind == "confirmation_schedule")
        self.assertEqual(schedule.progress.next_query_at, self.clock[0] + 1)

    def test_uncertain_after_full_never_logs_a_stale_running_summary(self):
        self.joiner.preflight((TARGET,), threading.Event())
        self.client.result = JoinRejected("11012017")
        for _ in range(2):
            self.joiner.direct_step(TARGET, threading.Event())
        self.client.result = JoinUncertain("synthetic POST timeout")
        self.joiner.direct_step(TARGET, threading.Event())
        self.joiner.close()
        start = next(i for i, e in enumerate(self.events) if e.kind == "verifying")
        tail = self.events[start:]
        self.assertFalse(any(e.kind in {"join_summary", "join_progress"} for e in tail))
        self.assertNotIn("11012017", " ".join(e.message for e in tail))
        self.assertIn("synthetic POST timeout", tail[0].message)


class ManualReviewTests(RecoveryFixture):
    def test_readonly_review_keeps_negative_state_locked_without_creation(self):
        op = self.reserve()
        review_pending(self.client, self.journal, TARGET, op, self.events.append)
        self.assertEqual(self.journal.get(TARGET)["state"], "SENDING")
        self.assertFalse(self.client.creates)

    def test_explicit_manual_release_rechecks_official_state_and_does_not_create(self):
        op = self.reserve()
        self.client.detail = Mock(wraps=self.client.detail)
        review_pending(self.client, self.journal, TARGET, op, self.events.append, manual_release=True)
        self.client.detail.assert_called_once_with(TARGET)
        self.assertEqual(self.journal.get(TARGET)["state"], "CANCELLED")
        self.assertFalse(self.client.creates)

    def test_manual_release_confirmed_join_records_success_instead(self):
        op = self.reserve()
        self.client.joined = True
        review_pending(self.client, self.journal, TARGET, op, self.events.append, manual_release=True)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertEqual(self.events[-1].confirmation.operation_id, op)
        self.assertFalse(self.client.creates)

    def test_other_owner_or_changed_operation_cannot_release(self):
        op = self.reserve()
        self.client.detail = Mock(wraps=self.client.detail)
        for journal, operation in ((Journal(self.path, "other-owner"), op), (self.journal, "outdated-operation")):
            with self.assertRaises(RequestFailure):
                review_pending(self.client, journal, TARGET, operation, self.events.append, manual_release=True)
        self.client.detail.assert_not_called()
        self.assertEqual(self.journal.get(TARGET)["state"], "SENDING")

    def test_operation_change_during_read_cannot_unlock_new_operation(self):
        op = self.reserve()
        def changed(target):
            self.journal.transition(target, "REJECTED", operation=op)
            self.reserve()
            return AccountDetail(snapshot(), False, None)
        self.client.detail = changed
        with self.assertRaises(RequestFailure):
            review_pending(self.client, self.journal, TARGET, op, self.events.append, manual_release=True)
        self.assertNotEqual(self.journal.get(TARGET)["operation_id"], op)
        self.assertEqual(self.journal.get(TARGET)["state"], "SENDING")


class MigrationTests(unittest.TestCase):
    def test_old_database_migration_preserves_reservation_and_reports_unknown_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                with db:
                    db.execute("CREATE TABLE joins(target TEXT PRIMARY KEY,state TEXT,amount TEXT,attempts INTEGER,operation_id TEXT,payload_hash TEXT,copy_id TEXT,updated REAL,owner_key TEXT)")
                    db.execute("INSERT INTO joins VALUES (?,?,?,?,?,?,?,?,?)", (TARGET, "UNCERTAIN", "300", 17925, "old-operation", "old-digest", None, 1, "test-owner"))
            self.assertEqual(len(pending_records(path)), 1)  # Read-only inspection also works before migration.
            journal = Journal(path, "test-owner")
            record = journal.get(TARGET)
            self.assertEqual((record["operation_id"], record["amount"], record["attempts"], record["owner_key"]),
                             ("old-operation", "300", 17925, "test-owner"))
            client, events = FakeClient(), []
            client.joined = True
            review_pending(client, journal, TARGET, "old-operation", events.append)
            self.assertIn("历史模式未记录", success_text(events[0]))
            self.assertEqual(events[0].confirmation.amount, Decimal("300"))
            self.assertFalse(client.creates)


class RecoveryUITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        root = self.root = tk.Tk()
        root.withdraw()
        self.addCleanup(destroy_root, root)
        with patch("binance_monitor.gui.DATA_DIR", self.path), \
             patch("binance_monitor.trade_panel.DATA_DIR", self.path), \
             patch("binance_monitor.gui.get_logger", return_value=Mock()), \
             patch("binance_monitor.gui.load_config", return_value={}), \
             patch("binance_monitor.gui.detect_local_proxy", return_value=""):
            self.app = App(root)

    def test_phase_and_logs_describe_confirmation_and_stop_accurately(self):
        app = self.app
        app.worker = Mock(is_alive=Mock(return_value=True))
        app.monitor = Mock(stop=threading.Event())
        app.events.put(Event("verifying", TARGET, message="创建接口超时，继续核对。"))
        app.events.put(Event("confirmation_waiting", TARGET, message="核对中，不重复提交。"))
        app.poll()
        self.assertEqual(app.status.get(), "结果核对中 · 不重复提交")
        self.assertIn("创建接口超时", app.log.get("1.0", "end"))
        app.events.put(Event("confirmation_paused", TARGET, message="核对已暂停。"))
        app.events.put(Event("stopped"))
        app.worker.is_alive.return_value = False
        app.poll()
        self.assertEqual(app.status.get(), "已停止")
        self.assertEqual(app.join_status[TARGET], "已暂停")

    def test_live_countdown_refreshes_without_extra_log_entries(self):
        app = self.app
        app.worker = Mock(is_alive=Mock(return_value=True))
        app.monitor = Mock(stop=threading.Event())
        app.events.put(Event("confirmation_waiting", TARGET, message="本轮已查询 4 次 | 官方尚未显示已跟单",
                             progress=ConfirmationProgress(4, "官方尚未显示已跟单", next_query_at=130)))
        with patch("binance_monitor.gui.time.monotonic", return_value=100):
            app.poll()
        self.assertEqual(app.status.get(), "核对 4 次 · 30 秒后查")
        before = app.log.get("1.0", "end")
        with patch("binance_monitor.gui.time.monotonic", return_value=105.2):
            app.poll()
        self.assertEqual(app.status.get(), "核对 4 次 · 25 秒后查")
        self.assertEqual(app.log.get("1.0", "end"), before)
        with patch("binance_monitor.gui.time.monotonic", return_value=135):
            app.poll()
        self.assertEqual(app.status.get(), "核对 4 次 · 等待查询")

    def test_subsecond_countdown_matches_point_one_setting(self):
        app = self.app
        app.worker = Mock(is_alive=Mock(return_value=True))
        app.monitor = Mock(stop=threading.Event())
        app.events.put(Event("confirmation_waiting", TARGET, message="核对中，抢位暂停",
                             progress=ConfirmationProgress(4, "尚未显示已跟单", next_query_at=10.1)))
        with patch("binance_monitor.gui.time.monotonic", return_value=10):
            app.poll()
        self.assertEqual(app.status.get(), "核对 4 次 · 0.1 秒后查")

    def test_inflight_query_shows_elapsed_time_and_pause_clears_it(self):
        app = self.app
        app.worker = Mock(is_alive=Mock(return_value=True))
        app.monitor = Mock(stop=threading.Event())
        app.events.put(Event("confirmation_checking", TARGET,
                             progress=ConfirmationProgress(4, "官方尚未显示已跟单", query_started_at=100)))
        before = app.log.get("1.0", "end")
        with patch("binance_monitor.gui.time.monotonic", return_value=103):
            app.poll()
        self.assertEqual(app.status.get(), "核对第 5 次 · 已用 3 秒")
        self.assertEqual(app.log.get("1.0", "end"), before)
        app.events.put(Event("confirmation_paused", TARGET, message="核对已暂停"))
        app.monitor.stop.set()
        app.poll()
        self.assertNotIn(TARGET, app.confirmation_progress)
        self.assertEqual(app.status.get(), "正在停止，等待请求核对")

    def test_joined_target_does_not_leave_stale_countdown(self):
        app = self.app
        app.worker = Mock(is_alive=Mock(return_value=True))
        app.monitor = Mock(stop=threading.Event())
        app.auto_active = True
        app.events.put(Event("confirmation_waiting", TARGET, message="查询进度",
                             progress=ConfirmationProgress(4, "尚未显示已跟单", next_query_at=130)))
        app.events.put(Event("joined", TARGET, message="已确认成功"))
        app.poll()
        self.assertNotIn(TARGET, app.confirmation_progress)
        self.assertEqual(app.status.get(), "全天抢位中")  # Other configured targets may still be running.

    def test_recovery_is_contextual_and_manual_release_requires_confirmation(self):
        app = self.app
        self.assertEqual(app.recovery_button.winfo_manager(), "")
        journal = Journal(self.path / "joins.sqlite3", "test-owner")
        journal.reserve(TARGET, Decimal("300"), JoinPolicy(None, "300"), {})
        app._refresh_recovery()
        self.assertEqual(app.recovery_button.winfo_manager(), "grid")
        app.review_records()
        window = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        buttons = []
        def children(widget):
            for child in widget.winfo_children():
                buttons.append(child)
                children(child)
        children(window)
        release = next(w for w in buttons if w.winfo_class() == "TButton" and w.cget("text") == "确认未创建并解除锁定")
        with patch("binance_monitor.gui.messagebox.askyesno", return_value=False) as confirm, \
             patch.object(app, "_account_task") as task:
            release.invoke()
        confirm.assert_called_once()
        task.assert_not_called()
        self.assertEqual(journal.get(TARGET)["state"], "SENDING")


if __name__ == "__main__":
    unittest.main()

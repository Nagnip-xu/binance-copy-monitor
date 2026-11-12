"""Offline regressions for the simplified real-money controls and direct loop."""
from __future__ import annotations

import tempfile
import threading
import time
import tkinter as tk
import unittest
from decimal import Decimal
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from binance_monitor.core import Monitor, RequestFailure, Settings
from binance_monitor.gui import App
from binance_monitor.private_api import JoinNotSent, JoinRejected
from binance_monitor.trading import AutoJoiner, JoinPolicy, Journal
from tests.test_monitor import ControlledStop
from tests.test_notifications_power import destroy_root
from tests.test_trading import FakeClient, TARGET, OTHER, snapshot


class ContinuousJoinTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = Journal(Path(self.directory.name) / "joins.sqlite3", "test-owner")
        self.client = FakeClient()
        self.client.balance = Mock(wraps=self.client.balance)
        self.client.detail = Mock(wraps=self.client.detail)
        self.policy = JoinPolicy(None, "300", direct_retry=True, max_attempts=None)
        self.events = []
        self.joiner = AutoJoiner(self.client, self.policy, self.journal, self.events.append)
        self.addCleanup(self.joiner.close)
        self.public = Mock()
        self.public.fetch.side_effect = AssertionError("direct loop must not query public slots")

    def run_monitor(self, requests=10):
        stop = ControlledStop(requests)
        Monitor(Settings((TARGET,), interval=.1), self.events.append, client=self.public,
                joiner=self.joiner, stop=stop, clock=lambda: 0).run()
        return stop

    def test_full_responses_retry_without_slot_queries_then_confirm_once_and_stop(self):
        def submit(payload):
            self.client.creates.append(payload)
            if len(self.client.creates) < 3:
                raise JoinRejected("11012017")
            self.client.joined = True
        self.client.create = submit
        self.run_monitor()
        self.public.fetch.assert_not_called()
        self.client.balance.assert_called_once()
        self.assertEqual(self.client.detail.call_count, 2)  # startup and confirmation
        self.assertEqual(len(self.client.creates), 3)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertEqual(sum(event.kind == "joined" for event in self.events), 1)
        self.assertIn("all_joined", [event.kind for event in self.events])
        self.assertEqual(self.events[-1].kind, "stopped")

    def test_unlimited_attempts_survive_old_count_and_unrelated_historical_investment(self):
        historical = JoinPolicy("1000", "300")
        self.journal.reserve(OTHER, Decimal("300"), historical, {})
        self.journal.transition(OTHER, "JOINED", copy_id="9000000000000000001")
        self.journal.reserve(TARGET, Decimal("300"), self.policy, {})
        self.journal.transition(TARGET, "REJECTED")
        with self.journal._connect() as db:
            db.execute("UPDATE joins SET attempts=10001 WHERE target=?", (TARGET,))
        self.run_monitor()
        self.assertEqual(self.journal.get(TARGET)["attempts"], 10002)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")

    def test_stop_during_full_loop_keeps_retry_count_and_reports_final_summary(self):
        self.client.result = JoinRejected("11012017")
        stop = self.run_monitor(3)
        self.assertEqual(len(self.client.creates), 3)
        self.assertEqual(stop.waits, [.1, .1, .1])
        progress = [event.message for event in self.events if event.kind in {"join_progress", "join_summary"}]
        self.assertIn("已提交 1 次", progress[0])
        self.assertIn("已提交 3 次", progress[-1])
        self.assertIn("11012017", progress[-1])
        self.assertIn("已停止", progress[-1])
        self.assertNotIn("抢位中", progress[-1])
        self.assertNotIn("joined", [event.kind for event in self.events])
        self.assertEqual(self.journal.get(TARGET)["state"], "REJECTED")

    def test_direct_startup_template_keeps_working_after_a_day_without_public_refresh(self):
        stop = threading.Event()
        self.joiner.preflight((TARGET,), stop)
        template = self.joiner.startup_snapshots[TARGET]
        self.joiner.startup_snapshots[TARGET] = replace(template, checked_at=time.time() - 86400)
        self.joiner.direct_step(TARGET, stop)
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")

    def test_transient_startup_query_retries_before_any_create_and_reuses_lock(self):
        self.client.balance.side_effect = [RequestFailure("synthetic temporary network failure"), Decimal("600")]
        self.run_monitor()
        self.assertEqual(self.client.balance.call_count, 2)
        self.assertEqual(len(self.client.creates), 1)
        self.assertIn("cooldown", [event.kind for event in self.events])
        with self.journal.exclusive():
            pass  # the run also released its process lock

    def test_proven_unsent_transient_failure_releases_reservation_then_resumes(self):
        calls = []
        def submit(payload):
            calls.append(payload)
            if len(calls) == 1:
                raise JoinNotSent("synthetic identity read timeout before create")
            self.client.creates.append(payload)
            self.client.joined = True
        self.client.create = submit
        self.run_monitor()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.journal.get(TARGET)["attempts"], 1)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertIn("cooldown", [event.kind for event in self.events])

    def test_unlimited_policy_still_checks_ceiling_and_real_free_balance(self):
        for ceiling, balance in (("299", "600"), ("300", "299")):
            with self.subTest(ceiling=ceiling, balance=balance):
                self.joiner.policy = JoinPolicy(None, ceiling, direct_retry=True, max_attempts=None)
                self.client.balance.return_value = Decimal(balance)
                self.run_monitor()
                self.assertFalse(self.client.creates)
                self.assertIn("halted", [event.kind for event in self.events])

    def test_transient_network_failures_use_short_backoff_and_coalesced_progress(self):
        self.client.result = JoinNotSent("synthetic temporary identity timeout")
        stop = self.run_monitor(3)
        self.assertEqual(stop.waits, [.5, 1.0, 2.0])
        self.assertNotIn("error", [event.kind for event in self.events])
        progress = [event.message for event in self.events if event.kind in {"join_progress", "join_summary"}]
        self.assertEqual(len(progress), 2)  # first retry and final stop summary
        self.assertIn("已提交 0 次", progress[-1])
        self.assertIn("查询重试 3 次", progress[-1])
        self.assertIn("最近拒绝：—", progress[-1])
        self.assertNotIn("11012017", progress[-1])
        self.assertEqual(self.journal.get(TARGET)["attempts"], 0)

    def test_direct_retry_respects_server_cooldown(self):
        self.client.result = JoinNotSent("synthetic rate limit before create", retry_after=90)
        stop = self.run_monitor(1)
        self.assertEqual(stop.waits, [90])
        self.assertTrue(any(event.kind == "cooldown" and event.retry_after == 90 for event in self.events))

    def test_fatal_error_remains_visible_and_halts_instead_of_being_summarized_away(self):
        self.client.result = JoinNotSent("synthetic login expired", fatal=True)
        stop = self.run_monitor()
        self.assertEqual(stop.waits, [])
        self.assertIn("error", [event.kind for event in self.events])
        self.assertIn("halted", [event.kind for event in self.events])


class SimpleControlsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)

    def make_app(self, config=None):
        with patch("binance_monitor.gui.get_logger", return_value=Mock()), \
             patch("binance_monitor.gui.load_config", return_value=config or {}), \
             patch("binance_monitor.gui.detect_local_proxy", return_value=""), \
             patch("binance_monitor.gui.DATA_DIR", Path(self.directory.name)), \
             patch("binance_monitor.trade_panel.DATA_DIR", Path(self.directory.name)):
            return App(self.root)

    def test_exact_two_tabs_and_default_real_direct_continuous_policy(self):
        app = self.make_app()
        self.assertEqual([app.notebook.tab(tab, "text") for tab in app.notebook.tabs()], ["监控配置", "本机登录"])
        self.assertTrue(app.real.get())
        self.assertTrue(app.direct.get())
        self.assertEqual(app._settings().interval, .1)
        policy = app._policy(app._settings())
        self.assertIsNone(policy.total_budget)
        self.assertIsNone(policy.max_attempts)
        self.assertEqual(policy.margin_mode, "FOLLOW_LEAD")
        self.assertEqual(policy.leverage_mode, "FOLLOW_LEAD")
        payload = policy.payload(snapshot())
        for key in ("leverageValue", "totalStopLossRate", "takeProfitRate", "stopLostRate", "maxPositionPerSymbolRate"):
            self.assertIsNone(payload[key])
        self.assertEqual(payload["investAmount"], "300")

    def test_old_financial_controls_do_not_survive_as_hidden_limits_or_risk(self):
        app = self.make_app({"targets": [TARGET], "ceiling": "400", "interval": 2,
                             "trade": {"values": {"budget": "200", "cap": "200", "max_attempts": "100",
                                                  "margin": "逐仓", "leverage_value": "5", "stop_loss": "5"}}})
        self.assertEqual(app._settings().interval, .1)
        policy = app._policy(app._settings())
        self.assertEqual(policy.per_target_cap, Decimal("400"))
        self.assertIsNone(policy.total_budget)
        self.assertIsNone(policy.max_attempts)
        self.assertEqual(policy.margin_mode, "FOLLOW_LEAD")
        self.assertIsNone(policy.stop_loss)

    def test_saving_keeps_only_simple_controls_and_account_query(self):
        app = self.make_app()
        app.interval.set("0.5")
        app.ceiling.set("400")
        app.copy_model.set("定额跟单（每笔10 USDT）")
        with patch("binance_monitor.gui.save_config") as save:
            app.save()
        config = save.call_args.args[0]
        self.assertEqual(config["version"], 2)
        self.assertEqual(config["interval"], .5)
        self.assertEqual(config["copy_model"], "FIXED_AMT")
        self.assertNotIn("values", config["trade"])
        self.assertNotIn("budget", repr(config))
        policy = app._policy(app._settings())
        self.assertEqual(policy.cost_per_order, Decimal("10"))
        self.assertEqual(policy.amount(snapshot()), Decimal("400"))

    def test_log_format_matches_reference_and_no_attempt_spam(self):
        app = self.make_app()
        app._append("示例(123456) 直接抢位中 | 已提交 10357 次 | 最近拒绝：币安业务错误 11012017")
        text = app.log.get("1.0", "end")
        self.assertRegex(text, r"\d{2}:\d{2}:\d{2} \[INFO\] 示例\(123456\) 直接抢位中 \| 已提交 10357 次")
        self.assertEqual(app.log.cget("background"), "#080808")

    def test_start_uses_real_client_by_default_but_does_not_start_on_load(self):
        app = self.make_app()
        self.assertIsNone(app.worker)
        client = Mock()
        client.session.fingerprint = "synthetic-owner"
        with patch.object(app.trade, "client", return_value=client) as account, \
             patch("binance_monitor.gui.Monitor") as monitor, \
             patch("binance_monitor.gui.threading.Thread") as thread, \
             patch("binance_monitor.gui.DATA_DIR", Path(self.directory.name)):
            app.start()
        account.assert_called_once()
        thread.return_value.start.assert_called_once()
        joiner = monitor.call_args.kwargs["joiner"]
        self.assertIs(joiner.client, client)
        self.assertTrue(joiner.policy.direct_retry)
        self.assertIsNone(joiner.policy.max_attempts)
        client.create.assert_not_called()

    def test_account_query_completion_does_not_claim_the_monitor_stopped(self):
        from binance_monitor.core import Event
        app = self.make_app()
        app.events.put(Event("account_done"))
        app.events.put(Event("cooldown", retry_after=.5))
        app.poll()
        text = app.log.get("1.0", "end")
        self.assertIn("账户操作已结束", text)
        self.assertNotIn("监控已停止", text)
        self.assertNotIn("[INFO] \n", text)


if __name__ == "__main__":
    unittest.main()

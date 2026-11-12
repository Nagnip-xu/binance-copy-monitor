from __future__ import annotations

import tempfile
import threading
import tkinter as tk
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from binance_monitor.core import Event, JoinConfirmation, Monitor, RequestFailure, Settings, Snapshot
from binance_monitor.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake
from binance_monitor.success_view import SuccessNotifications, success_text


TARGET = "5075281354358777856"
OTHER = "5075281354358777857"


def destroy_root(root):
    for child in list(root.winfo_children()):
        if isinstance(child, tk.Toplevel):
            child.destroy()
    for job in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(job)
    root.destroy()


def success_event(target=TARGET, operation="synthetic-operation"):
    snapshot = Snapshot(target, "模拟带单员", 999, 1000, Decimal("300"), "ACTIVE", 0)
    return Event("joined", target, snapshot,
                 confirmation=JoinConfirmation(operation, "9000000000000000001", "FIXED_RATIO", Decimal("350.12345678")))


class PowerTests(unittest.TestCase):
    def test_request_and_release_on_same_worker_even_after_error(self):
        calls = []
        states = []
        def setter(flags):
            calls.append((threading.get_ident(), flags))
            return ES_CONTINUOUS
        def run():
            try:
                with KeepAwake(True, setter=setter) as power:
                    states.append(power.active)
                    raise RuntimeError("synthetic")
            except RuntimeError:
                states.append("error propagated")
        worker = threading.Thread(target=run)
        worker.start()
        worker.join()
        self.assertEqual([flags for _, flags in calls], [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS])
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertNotEqual(calls[0][0], threading.get_ident())
        self.assertEqual(states, [True, "error propagated"])

    def test_disabled_and_failed_requests_do_not_claim_or_release(self):
        setter = Mock(return_value=0)
        with KeepAwake(False, setter=setter) as power:
            self.assertFalse(power.active)
        setter.assert_not_called()
        with KeepAwake(True, setter=setter) as power:
            self.assertFalse(power.active)
        setter.assert_called_once_with(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    def test_monitor_releases_sleep_request_after_fatal_failure(self):
        client = Mock()
        client.fetch.side_effect = RequestFailure("synthetic", fatal=True)
        guard = Mock()
        guard.__enter__ = Mock(return_value=Mock(active=True))
        guard.__exit__ = Mock(return_value=False)
        events = []
        with patch("binance_monitor.core.KeepAwake", return_value=guard) as factory:
            Monitor(Settings((TARGET,), keep_awake=True), events.append, client=client).run()
        factory.assert_called_once_with(True)
        guard.__exit__.assert_called_once()
        self.assertIn("power", [event.kind for event in events])
        self.assertEqual(events[-1].kind, "stopped")

    def test_demo_never_requests_keep_awake(self):
        with patch("binance_monitor.core.KeepAwake") as factory:
            Monitor(Settings((TARGET,), keep_awake=True, demo=True), lambda _: None).run(once=True)
        factory.assert_called_once_with(False)


class NotificationTests(unittest.TestCase):
    def test_receipt_text_is_exact_and_demo_is_clear(self):
        text = success_text(success_event())
        self.assertIn("350.12345678 USDT", text)
        self.assertIn("等比跟单", text)
        self.assertIn("【演示数据】", success_text(success_event(), demo=True))
        fixed = replace(success_event(), confirmation=replace(success_event().confirmation, copy_model="FIXED_AMT"))
        self.assertIn("定额跟单", success_text(fixed))

    def test_no_success_receipt_for_existing_or_unconfirmed_results(self):
        for kind in ("available", "joining", "verifying", "joined_existing", "error"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                success_text(replace(success_event(), kind=kind))

    def test_popup_deduplicates_and_remains_nonmodal_for_multiple_targets(self):
        root = tk.Tk()
        root.withdraw()
        try:
            notices = SuccessNotifications(root)
            window = notices.show(success_event())
            root.update()
            self.assertTrue(window.winfo_exists())
            self.assertIsNone(root.grab_current())
            self.assertIsNone(notices.show(success_event()))
            self.assertIsNone(notices.show(replace(success_event(), kind="joined_existing")))
            second = notices.show(success_event(OTHER, "second-operation"))
            root.update()
            self.assertEqual(len(notices.windows), 2)
            self.assertIsNot(window, second)
            processed = []
            root.after(0, lambda: processed.append("still polling"))
            root.update()
            self.assertTrue(processed)
            window.destroy()
            self.assertEqual(len(notices.windows), 1)
        finally:
            destroy_root(root)

    def test_app_handles_success_and_later_events_without_dismissing_popup(self):
        from binance_monitor.gui import App
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            try:
                with patch("binance_monitor.gui.get_logger", return_value=Mock()), \
                     patch("binance_monitor.gui.load_config", return_value={}), \
                     patch("binance_monitor.gui.detect_local_proxy", return_value=""), \
                     patch("binance_monitor.trade_panel.DATA_DIR", Path(directory)):
                    app = App(root)
                app.settings = Settings((TARGET,), sound=False)
                app.events.put(success_event())
                app.events.put(Event("all_joined", message="全部目标已确认"))
                app.events.put(Event("stopped"))
                app.poll()
                self.assertEqual(len(app.success_notifications.windows), 1)
                self.assertEqual(app.join_status[TARGET], "加入成功")
                self.assertIn("全部目标已确认", app.log.get("1.0", "end"))
                self.assertEqual(app._settings().keep_awake, True)
            finally:
                destroy_root(root)


if __name__ == "__main__":
    unittest.main()

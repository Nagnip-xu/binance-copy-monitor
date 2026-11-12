"""Read-only target presentation stays exact and follows actual query results."""
import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from binance_monitor.core import Event
from binance_monitor.gui import App
from tests.test_notifications_power import destroy_root
from tests.test_trading import TARGET, OTHER, snapshot


class TargetViewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        with patch("binance_monitor.gui.DATA_DIR", Path(directory.name)), \
             patch("binance_monitor.trade_panel.DATA_DIR", Path(directory.name)), \
             patch("binance_monitor.gui.get_logger", return_value=Mock()), \
             patch("binance_monitor.gui.load_config", return_value={}), \
             patch("binance_monitor.gui.detect_local_proxy", return_value=""):
            self.app = App(self.root)
        self.table = self.app.target_table

    def values(self, target=TARGET):
        return tuple(self.table.tree.item(target, "values"))

    def test_configured_target_is_visible_before_any_queries_without_inventing_data(self):
        self.assertEqual(self.values(), (TARGET, "待查询", "—", "—", "—"))
        self.assertEqual([self.table.tree.heading(column, "text") for column in self.table.tree["columns"]],
                         ["Portfolio ID", "带单员", "名额", "最低金额", "现货余额"])

    def test_startup_events_populate_all_five_columns_with_exact_amounts(self):
        self.app.events.put(Event("balance", message="302.90619161"))
        self.app.events.put(Event("target_detail", TARGET, snapshot(1000, nickname="熬鹰资本"), "启动"))
        self.app.poll()
        self.assertEqual(self.values(), (TARGET, "熬鹰资本", "1000/1000（启动）", "300 USDT", "302.90619161 USDT"))

    def test_new_query_updates_capacity_and_old_retry_template_cannot_overwrite_it(self):
        old = snapshot(1000, checked_at=10)
        self.app.events.put(Event("target_detail", TARGET, old, "启动"))
        self.app.events.put(Event("target_detail", TARGET, replace(old, copiers=999, checked_at=20)))
        self.app.events.put(Event("join_progress", TARGET, old, "抢位进度"))
        self.app.poll()
        self.assertEqual(self.values()[2], "999/1000")

    def test_model_selection_uses_correct_minimum_and_keeps_unknown_explicit(self):
        self.table.update_snapshot(snapshot(minimum_fixed=Decimal("400.12345678")))
        self.table.set_model("FIXED_AMT")
        self.assertEqual(self.values()[3], "400.12345678 USDT")
        self.table.update_snapshot(snapshot(minimum_fixed=None, checked_at=10**12))
        self.assertEqual(self.values()[3], "未知")

    def test_target_changes_reorder_rows_and_clear_removed_targets(self):
        self.table.set_targets((TARGET, OTHER))
        self.table.update_balance("0")
        self.table.update_snapshot(snapshot())
        self.table.set_targets((OTHER,))
        self.assertEqual(self.table.tree.get_children(), (OTHER,))
        self.assertNotIn(TARGET, self.table.snapshots)
        self.assertEqual(self.values(OTHER)[4], "0 USDT")

    def test_account_reset_removes_previous_account_data(self):
        self.table.update_snapshot(snapshot())
        self.table.update_balance("302.9")
        self.table.set_targets((TARGET,), reset=True)
        self.assertEqual(self.values(), (TARGET, "待查询", "—", "—", "—"))

    def test_connection_test_shows_data_without_issuing_additional_requests(self):
        client = Mock()
        client.balance.return_value = Decimal("302.90619161")
        client.detail.return_value = Mock(snapshot=snapshot(), has_copy=False)
        with patch.object(self.app, "_account_task", side_effect=lambda action: action(client, self.app._settings())):
            self.app.test_connection()
        self.app.poll()
        self.assertEqual(self.values()[4], "302.90619161 USDT")
        self.assertEqual(self.values()[2], "999/1000")
        client.balance.assert_called_once()
        client.detail.assert_called_once_with(TARGET)
        client.create.assert_not_called()

    def test_many_targets_scroll_in_bounded_height(self):
        targets = tuple(str(5075281354358777800 + index) for index in range(20))
        self.table.set_targets(targets)
        self.assertEqual(len(self.table.tree.get_children()), 20)
        self.assertEqual(int(self.table.tree.cget("height")), 3)
        self.table.tree.see(targets[-1])
        self.assertEqual(self.values(targets[-1])[0], targets[-1])


if __name__ == "__main__":
    unittest.main()

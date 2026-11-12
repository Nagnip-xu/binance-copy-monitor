"""Account import must keep matched session/query data and never start trading."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from binance_monitor.credentials import Session
from binance_monitor.private_api import BalanceQuery
from binance_monitor.trade_panel import TradePanel


URL = "https://www.binance.com/bapi/asset/v3/private/asset-service/asset/get-user-asset"


def curl(url=URL, *, csrf="synthetic-new"):
    return ("curl '" + url + "' -H 'csrftoken: " + csrf + "' -b 'sid=synthetic' "
            "--data-raw '{\"needBtcValuation\":true,\"quoteAsset\":\"USDT\"}'")


class ImportFlowTests(unittest.TestCase):
    def panel(self, text):
        panel = object.__new__(TradePanel)
        panel.app = SimpleNamespace(root=None, _append=Mock(), test_connection=Mock(), start=Mock())
        panel.persist = Mock()
        panel.persist.get.return_value = False
        panel.mode = Mock()
        panel.import_text = Mock()
        panel.import_text.get.return_value = text
        panel._status = Mock()
        panel.session = None
        panel.balance_query = None
        return panel

    @patch("binance_monitor.trade_panel.messagebox.showerror")
    @patch("binance_monitor.trade_panel.save_session")
    def test_one_request_imports_both_then_tests_with_trading_disabled(self, save, error):
        panel = self.panel(curl())
        panel.import_and_test()
        self.assertIsNotNone(panel.session)
        self.assertEqual(panel.balance_query, BalanceQuery(URL, "POST", {"needBtcValuation": True, "quoteAsset": "USDT"}))
        panel.app.start.assert_not_called()
        panel.import_text.delete.assert_called_once_with("1.0", "end")
        panel.app.test_connection.assert_called_once_with()
        save.assert_not_called()
        error.assert_not_called()

    @patch("binance_monitor.trade_panel.messagebox.showerror")
    @patch("binance_monitor.trade_panel.save_session")
    def test_old_login_request_cannot_replace_balance_session_or_saved_login(self, save, error):
        panel = self.panel(curl(csrf="synthetic-old"))
        session = panel.session = Session({"cookie": "sid=synthetic", "csrftoken": "synthetic-new"})
        query = panel.balance_query = BalanceQuery(URL, "POST", {"quoteAsset": "USDT"})
        panel.persist.get.return_value = True
        self.assertFalse(panel._import(balance=False))
        self.assertIs(panel.session, session)
        self.assertIs(panel.balance_query, query)
        save.assert_not_called()
        panel.app.start.assert_not_called()
        panel.import_text.delete.assert_not_called()
        panel.app.test_connection.assert_not_called()
        error.assert_called_once()

    @patch("binance_monitor.trade_panel.messagebox.showerror")
    def test_unsupported_request_does_not_replace_or_test_existing_account(self, error):
        panel = self.panel(curl("https://www.binance.com/bapi/asset/v1/private/asset-service/withdraw"))
        session = panel.session = Session({"cookie": "sid=existing", "csrftoken": "synthetic-existing"})
        query = panel.balance_query = BalanceQuery(URL, "POST", {"quoteAsset": "USDT"})
        panel.import_and_test()
        self.assertIs(panel.session, session)
        self.assertIs(panel.balance_query, query)
        panel.app.test_connection.assert_not_called()
        error.assert_called_once()

    @patch("binance_monitor.trade_panel.messagebox.showerror")
    @patch("binance_monitor.trade_panel.save_session", side_effect=OSError("synthetic write failure"))
    def test_failed_encrypted_save_does_not_half_import_or_start_test(self, save, error):
        panel = self.panel(curl())
        panel.persist.get.return_value = True
        panel.import_and_test()
        self.assertIsNone(panel.session)
        self.assertIsNone(panel.balance_query)
        panel.app.test_connection.assert_not_called()
        error.assert_called_once()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from binance_monitor.browser_account import (BrowserAccount, BrowserClient, BrowserIdentity,
                                             IDENTITY_URL, ORIGIN, account_identity)
from binance_monitor.core import RequestFailure, ValidationError
from binance_monitor.private_api import CREATE_PATH, JoinNotSent, JoinUncertain, SPOT_ASSET_V3_PATH


def identity_payload(uid="123456789"):
    return {"code": "000000", "success": True, "data": {"userId": uid}}


class BrowserIdentityTests(unittest.TestCase):
    def test_uid_is_stable_across_relogin_but_distinct_between_accounts(self):
        first = account_identity(identity_payload())
        self.assertEqual(first, account_identity(identity_payload(123456789)))
        self.assertNotEqual(first, account_identity(identity_payload("987654321")))
        self.assertNotIn("123456789", first.fingerprint)
        self.assertNotIn(first.fingerprint, repr(first))

    def test_no_identity_guess_from_analytics_email_device_or_conflicting_uids(self):
        for data in ({"id": "12345"}, {"email": "local@example.test"}, {"deviceId": "12345"},
                     {"userId": True}, {"userId": 0}, {"userId": "not-a-uid"},
                     {"userId": "123", "uid": "456"}, {"userId": "123", "userInfo": {"userId": "456"}}):
            with self.subTest(data=data), self.assertRaises(RequestFailure):
                account_identity({"code": "000000", "data": data})
        with self.assertRaises(RequestFailure):
            account_identity({"code": "100001", "success": False})


class BrowserTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.account = BrowserAccount(Path(self.temp.name) / "edge-profile")
        self.identity = self.account.identity = account_identity(identity_payload())
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.account.close)

    def test_account_change_stops_before_balance_or_create(self):
        for joining, url, method, body in (
            (False, ORIGIN + SPOT_ASSET_V3_PATH, "POST", {"quoteAsset": "USDT"}),
            (True, ORIGIN + CREATE_PATH, "POST", {"investAmount": "300"}),
        ):
            self.account.identity = self.identity
            fetch = self.account._fetch = AsyncMock(return_value=identity_payload("987654321"))
            error = JoinNotSent if joining else RequestFailure
            with self.subTest(joining=joining), self.assertRaises(error):
                self.account.request(url, method=method, body=body, identity=self.identity, joining=joining)
            fetch.assert_awaited_once_with(IDENTITY_URL)
            self.assertIsNone(self.account.identity)

    def test_balance_uses_browser_cookie_session_and_existing_free_funds_parser(self):
        payload = {"code": "000000", "success": True, "data": [{"asset": "USDT", "free": "456.12345678", "locked": "100"}]}
        self.account._fetch = AsyncMock(side_effect=[identity_payload(), payload])
        client = BrowserClient(self.account, self.identity)
        self.assertEqual(client.balance(), Decimal("456.12345678"))
        self.assertEqual(client.session.fingerprint, self.identity.fingerprint)
        calls = self.account._fetch.await_args_list
        self.assertEqual(calls[0].args, (IDENTITY_URL,))
        self.assertEqual(calls[1].args, (ORIGIN + SPOT_ASSET_V3_PATH, "POST", {"needBtcValuation": True, "quoteAsset": "USDT"}))

    def test_redirect_origin_transfer_and_invalid_wallet_are_rejected_before_fetch(self):
        self.account._fetch = AsyncMock()
        for url, method, body in (
            ("https://evil.test" + SPOT_ASSET_V3_PATH, "POST", {}),
            (ORIGIN + "/bapi/asset/v1/private/asset-service/withdraw", "POST", {}),
            (ORIGIN + SPOT_ASSET_V3_PATH, "POST", {"walletType": "FUTURES"}),
            (ORIGIN + CREATE_PATH, "GET", None),
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.account.request(url, method=method, body=body, identity=self.identity)
        self.account._fetch.assert_not_awaited()

    def test_create_network_uncertainty_never_retries(self):
        self.account._fetch = AsyncMock(side_effect=[identity_payload(), RequestFailure("synthetic timeout")])
        client = BrowserClient(self.account, self.identity)
        with self.assertRaises(JoinUncertain):
            client.create({"leadPortfolioId": "5075281354358777856", "investAmount": "300"})
        self.assertEqual(self.account._fetch.await_count, 2)  # identity + exactly one creation

    def test_create_failure_keeps_safe_reason_and_retry_after_for_confirmation(self):
        self.account._fetch = AsyncMock(side_effect=[identity_payload(), RequestFailure("HTTP 429", retry_after=90)])
        with self.assertRaises(JoinUncertain) as raised:
            BrowserClient(self.account, self.identity).create({"investAmount": "300"})
        self.assertIn("HTTP 429", str(raised.exception))
        self.assertEqual(raised.exception.retry_after, 90)
        self.assertEqual(self.account._fetch.await_count, 2)

    def test_identity_timeout_proves_no_create_request_was_sent(self):
        self.account._fetch = AsyncMock(side_effect=RequestFailure("synthetic identity timeout"))
        client = BrowserClient(self.account, self.identity)
        with self.assertRaises(JoinNotSent) as raised:
            client.create({"leadPortfolioId": "5075281354358777856", "investAmount": "300"})
        self.assertFalse(raised.exception.fatal)
        self.account._fetch.assert_awaited_once_with(IDENTITY_URL)

    def cached_client(self):
        context = self.account._context = Mock()
        context.close = AsyncMock()
        context.cookies = AsyncMock(return_value=[{"name": "synthetic-auth", "value": "synthetic-session-one"}])
        self.account._official_page = AsyncMock(return_value=Mock(url=ORIGIN + "/wallet"))
        self.account._clock = Mock(return_value=100.0)
        def fetch(url, *_):
            if url == IDENTITY_URL:
                return identity_payload()
            if url == ORIGIN + CREATE_PATH:
                return {"code": "000000", "success": True}
            return {"code": "000000", "success": True, "data": [{"asset": "USDT", "free": "600"}]}
        self.account._fetch = AsyncMock(side_effect=fetch)
        return BrowserClient(self.account, self.identity), context

    def test_repeated_creates_reuse_verified_session_without_repeated_identity_http(self):
        client, context = self.cached_client()
        for _ in range(100):
            client.create({"leadPortfolioId": "5075281354358777856", "investAmount": "300"})
        calls = self.account._fetch.await_args_list
        self.assertEqual(sum(call.args[0] == IDENTITY_URL for call in calls), 1)
        self.assertEqual(sum(call.args[0] == ORIGIN + CREATE_PATH for call in calls), 100)
        self.assertEqual(context.cookies.await_count, 101)
        self.assertIsInstance(self.account._verified_signature, bytes)
        self.assertNotIn(b"synthetic-session-one", self.account._verified_signature)

    def test_session_change_forces_verification_and_stops_wrong_account_before_post(self):
        client, context = self.cached_client()
        client.create({"investAmount": "300"})
        context.cookies.return_value = [{"name": "synthetic-auth", "value": "synthetic-session-two"}]
        self.account._fetch.reset_mock()
        self.account._fetch.side_effect = lambda *_: identity_payload("987654321")
        with self.assertRaises(JoinNotSent) as raised:
            client.create({"investAmount": "300"})
        self.assertTrue(raised.exception.fatal)
        self.account._fetch.assert_awaited_once_with(IDENTITY_URL)
        self.assertIsNone(self.account.identity)

    def test_cache_expires_and_balance_confirmation_always_verifies_freshly(self):
        client, _ = self.cached_client()
        client.create({"investAmount": "300"})
        self.account._clock.return_value = 106.0
        client.create({"investAmount": "300"})
        self.assertEqual(client.balance(), Decimal("600"))
        calls = self.account._fetch.await_args_list
        self.assertEqual(sum(call.args[0] == IDENTITY_URL for call in calls), 3)

    def test_login_route_invalidates_cache_without_reading_form_contents(self):
        client, _ = self.cached_client()
        client.create({"investAmount": "300"})
        class LoginRequest:
            url = "https://accounts.binance.com/login"
            method = "POST"
            @property
            def post_data(self):
                raise AssertionError("never inspect authentication form content")
        self.account._session_request(LoginRequest())
        client.create({"investAmount": "300"})
        self.assertEqual(sum(call.args[0] == IDENTITY_URL for call in self.account._fetch.await_args_list), 2)

    def test_session_changes_during_verification_are_rechecked_before_create(self):
        client, context = self.cached_client()
        first = [{"name": "synthetic-auth", "value": "one"}]
        second = [{"name": "synthetic-auth", "value": "two"}]
        context.cookies.side_effect = [first, second, second, second]
        client.create({"investAmount": "300"})
        calls = self.account._fetch.await_args_list
        self.assertEqual(sum(call.args[0] == IDENTITY_URL for call in calls), 2)
        self.assertEqual(sum(call.args[0] == ORIGIN + CREATE_PATH for call in calls), 1)

    def test_unstable_session_never_creates_with_an_unverified_binding(self):
        client, context = self.cached_client()
        context.cookies.side_effect = [[{"name": "synthetic-auth", "value": str(i)}] for i in range(4)]
        with self.assertRaises(JoinNotSent) as raised:
            client.create({"investAmount": "300"})
        self.assertFalse(raised.exception.fatal)
        self.assertTrue(all(call.args[0] == IDENTITY_URL for call in self.account._fetch.await_args_list))

    def test_cookie_inspection_failure_does_not_use_cached_verification(self):
        client, context = self.cached_client()
        client.create({"investAmount": "300"})
        context.cookies.side_effect = RuntimeError("synthetic private material")
        self.account._fetch.reset_mock()
        with self.assertRaises(JoinNotSent) as raised:
            client.create({"investAmount": "300"})
        self.assertNotIn("private material", str(raised.exception))
        self.assertTrue(raised.exception.fatal)
        self.account._fetch.assert_not_awaited()

    def test_read_timeout_and_rate_limit_are_recoverable_but_login_failure_is_fatal(self):
        page = Mock()
        self.account._official_page = AsyncMock(return_value=page)
        self.account._headers = {"csrftoken": "synthetic"}
        for result, fatal, delay in (({"error": "network"}, False, 0),
                                      ({"status": 429}, False, 60),
                                      ({"status": 503}, False, 0),
                                      ({"status": 401}, True, 0)):
            with self.subTest(result=result):
                page.evaluate = AsyncMock(return_value=result)
                with self.assertRaises(RequestFailure) as raised:
                    self.account._call("_fetch", IDENTITY_URL)
                self.assertEqual(raised.exception.fatal, fatal)
                self.assertEqual(raised.exception.retry_after, delay)

    def test_query_deadline_is_shorter_but_create_deadline_is_preserved(self):
        page = Mock()
        page.evaluate = AsyncMock(return_value={"status": 200, "payload": {"code": "000000"}})
        self.account._official_page = AsyncMock(return_value=page)
        self.account._headers = {"csrftoken": "synthetic"}
        self.account._call("_fetch", IDENTITY_URL)
        self.assertEqual(page.evaluate.await_args.args[1]["timeoutMs"], 4000)
        self.account._call("_fetch", ORIGIN + CREATE_PATH, "POST", {})
        self.assertEqual(page.evaluate.await_args.args[1]["timeoutMs"], 8000)
        page.evaluate.return_value = {"status": 429, "retryAfter": "90"}
        with self.assertRaises(RequestFailure) as raised:
            self.account._call("_fetch", IDENTITY_URL)
        self.assertEqual(raised.exception.retry_after, 90)

    def test_observer_ignores_login_password_cookie_and_trade_data(self):
        request = Mock()
        request.method = "POST"
        request.url = ORIGIN + SPOT_ASSET_V3_PATH
        request.post_data = '{"quoteAsset":"USDT","needBtcValuation":true}'
        request.header_value = AsyncMock(side_effect=lambda name: "synthetic-csrf" if name == "csrftoken" else None)
        response = Mock(url=request.url, status=200, request=request)
        self.account._call("_observe", response)
        self.assertEqual(self.account._headers, {"csrftoken": "synthetic-csrf"})
        names = [call.args[0] for call in request.header_value.await_args_list]
        self.assertNotIn("cookie", names)
        self.assertTrue(self.account.balance_query.explicitly_spot)
        request.header_value.reset_mock()
        for url in (ORIGIN + CREATE_PATH, "https://accounts.binance.com/login", ORIGIN + "/login"):
            response.url = url
            self.account._call("_observe", response)
        request.header_value.assert_not_called()

    def test_fetch_error_does_not_return_raw_response_or_headers(self):
        page = Mock()
        page.evaluate = AsyncMock(return_value={"status": 401})
        self.account._official_page = AsyncMock(return_value=page)
        self.account._headers = {"csrftoken": "synthetic-secret"}
        with self.assertRaises(RequestFailure) as raised:
            self.account._call("_fetch", IDENTITY_URL)
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertIn("登录已失效", str(raised.exception))

    def test_close_stops_only_owned_context_and_is_idempotent(self):
        context = self.account._context = Mock()
        context.close = AsyncMock()
        self.account.close()
        context.close.assert_awaited_once()
        self.assertFalse(self.account._thread.is_alive())
        self.account.close()


if __name__ == "__main__":
    unittest.main()

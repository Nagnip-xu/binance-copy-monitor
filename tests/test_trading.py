from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from binance_monitor.core import RequestFailure, Snapshot, ValidationError
from binance_monitor.credentials import Session, import_request, load_session, save_session
from binance_monitor.private_api import AccountDetail, BalanceQuery, JoinRejected, JoinUncertain, NoRedirect, PrivateClient, parse_spot_usdt
from binance_monitor.trading import AutoJoiner, Journal, JoinPolicy


TARGET = "5075281354358777856"
OTHER = "5075281354358777857"
COPY_ID = "9000000000000000001"
BALANCE_URL = "https://www.binance.com/bapi/asset/v1/private/asset-service/wallet/balance"
SPOT_ASSET_V3_URL = "https://www.binance.com/bapi/asset/v3/private/asset-service/asset/get-user-asset"


def snapshot(count=999, **changes):
    value = Snapshot(TARGET, "test", count, 1000, Decimal("300"), "ACTIVE", time.time(), minimum_fixed=Decimal("400"))
    return replace(value, **changes)


def balance_payload(free="600.12345678"):
    return {"code": "000000", "success": True, "data": {"walletType": "SPOT", "balances": [{"asset": "USDT", "free": free, "locked": "100"}]}}


class CredentialsTests(unittest.TestCase):
    def test_edge_empty_headers_do_not_block_session_import(self):
        command = (
            "curl --url 'https://www.binance.com/bapi/accounts/v1/private/account/get-user-base-info' "
            "-H 'bnc-location;' -H 'x-passthrough-token: previous-value' "
            "-H 'x-passthrough-token;' -H 'csrftoken: local-csrf' -b 'sid=local-cookie'"
        )
        result = import_request(command)
        self.assertEqual(result.method, "GET")
        self.assertEqual(result.session.headers["cookie"], "sid=local-cookie")
        self.assertEqual(result.session.headers["csrftoken"], "local-csrf")
        self.assertNotIn("x-passthrough-token", result.session.headers)
        self.assertNotIn("bnc-location", result.session.headers)

    def test_malformed_empty_headers_are_still_rejected(self):
        for header in (";", "broken header;", "bnc-location;;", "missing-colon"):
            with self.subTest(header=header), self.assertRaises(ValidationError):
                import_request("curl 'https://www.binance.com/' -H 'csrftoken: csrf' -b 'sid=local' -H '" + header + "'")

    def test_imports_browser_curl_as_data_not_a_shell(self):
        text = "curl 'https://www.binance.com/bapi/test' \\\n  -H 'csrftoken: local-csrf' \\\n  -b 'sid=local-cookie' -H 'authorization: should-be-dropped' --compressed"
        result = import_request(text)
        self.assertEqual(result.method, "GET")
        self.assertEqual(result.session.headers["csrftoken"], "local-csrf")
        self.assertNotIn("authorization", result.session.headers)
        self.assertNotIn("local-cookie", repr(result))

    def test_rejects_non_binance_destinations_file_reads_and_shell_commands(self):
        commands = (
            "curl 'https://www.binance.com.evil.test/' -H 'csrftoken: x' -b 'sid=y'",
            "curl 'https://www.binance.com/' -H 'csrftoken: x' -b 'sid=y' ; echo test",
            "curl 'https://www.binance.com/' -H 'csrftoken: x' -b 'sid=y' --data @secrets.json",
            "curl 'https://www.binance.com/' -H 'csrftoken: x' -b 'sid=y' --proxy http://evil.test",
            "curl 'https://evil@www.binance.com/' -H 'csrftoken: x' -b 'sid=y'",
        )
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ValidationError):
                import_request(command)

    def test_json_import_and_header_injection_checks(self):
        value = import_request(json.dumps({"headers": {"Cookie": "sid=x", "csrftoken": "csrf"}}))
        self.assertEqual(value.session.headers["cookie"], "sid=x")
        with self.assertRaises(ValidationError):
            Session({"cookie": "x\r\nLeak: yes", "csrftoken": "x"})
        with self.assertRaises(ValidationError):
            Session({"cookie": "sid=x"})

    @unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI")
    def test_windows_session_roundtrip_is_encrypted_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.dpapi"
            session = Session({"cookie": "sid=local-test-secret", "csrftoken": "local-csrf"})
            save_session(session, path)
            self.assertNotIn(b"local-test-secret", path.read_bytes())
            self.assertEqual(load_session(path).headers, session.headers)


class BalanceTests(unittest.TestCase):
    def test_spot_v3_browser_request_imports_with_original_method_and_body(self):
        command = (
            "curl --url '" + SPOT_ASSET_V3_URL + "' "
            "-H 'bnc-location;' -H 'csrftoken: synthetic-csrf' -b 'sid=synthetic-cookie' "
            "-H 'x-passthrough-token;' --data-raw '{\"needBtcValuation\":true,\"quoteAsset\":\"USDT\"}'"
        )
        imported = import_request(command)
        query = BalanceQuery.from_import(imported)
        self.assertEqual(query.url, SPOT_ASSET_V3_URL)
        self.assertEqual(query.method, "POST")
        self.assertEqual(query.body, {"needBtcValuation": True, "quoteAsset": "USDT"})
        self.assertTrue(query.explicitly_spot)
        self.assertNotIn("cookie", repr(query).lower())

    def test_spot_v3_contract_rejects_conflicting_wallet_selectors(self):
        for url, body in (
            (SPOT_ASSET_V3_URL, {"walletType": "FUTURES"}),
            (SPOT_ASSET_V3_URL + "?walletType=FUTURES", {"walletType": "SPOT"}),
            (SPOT_ASSET_V3_URL + "?walletType=SPOT&walletType=FUTURES", {}),
            (SPOT_ASSET_V3_URL + "?type=SPOT", {"type": "FUNDING"}),
        ):
            with self.subTest(url=url, body=body), self.assertRaises(ValidationError):
                BalanceQuery(url, "POST", body)
        for url in (BALANCE_URL, SPOT_ASSET_V3_URL.replace('/v3/', '/v1/')):
            self.assertFalse(BalanceQuery(url, "POST", {"quoteAsset": "USDT"}).explicitly_spot)
        self.assertFalse(BalanceQuery(SPOT_ASSET_V3_URL).explicitly_spot)

    def test_v3_untagged_spot_balances_still_require_unique_available_usdt(self):
        query = BalanceQuery(SPOT_ASSET_V3_URL, "POST", {"needBtcValuation": True, "quoteAsset": "USDT"})
        for data in (
            [{"asset": "USDT", "walletType": "FUTURES", "free": "5000"}],
            [{"asset": "USDT", "btcValuation": "0.01", "quoteAssetValuation": "600"}],
            [{"asset": "USDT", "free": "100", "available": "100"}],
            [{"asset": "USDT", "free": "100"}, {"asset": "USDT", "free": "200"}],
        ):
            with self.subTest(data=data), self.assertRaises(RequestFailure):
                parse_spot_usdt({"code": "000000", "data": data}, request_is_spot=query.explicitly_spot)

    def test_untagged_balance_requires_explicit_spot_request_contract(self):
        raw = {"code": "000000", "data": [{"asset": "USDT", "free": "1000"}]}
        with self.assertRaises(RequestFailure):
            parse_spot_usdt(raw)
        self.assertEqual(parse_spot_usdt(raw, request_is_spot=True), Decimal("1000"))
        raw["data"][0]["accountType"] = "FUTURES"
        with self.assertRaises(RequestFailure):
            parse_spot_usdt(raw, request_is_spot=True)

    def test_free_spot_usdt_is_used_instead_of_locked_or_total(self):
        self.assertEqual(parse_spot_usdt(balance_payload()), Decimal("600.12345678"))

    def test_never_guesses_from_total_valuation_futures_or_multiple_matches(self):
        for data in (
            [{"asset": "USDT", "balance": "1000"}],
            [{"asset": "USDT", "free": "100", "available": "100"}],
            [{"asset": "USDT", "free": "NaN"}],
            [{"asset": "USDT", "free": "-1"}],
            [{"walletType": "FUTURES", "asset": "USDT", "free": "1000"}],
            [{"asset": "USDT", "free": "1000"}, {"asset": "USDT", "free": "2000"}],
        ):
            with self.subTest(data=data), self.assertRaises(RequestFailure):
                parse_spot_usdt({"code": "000000", "data": data})

    def test_balance_import_cannot_enable_transfer_or_create_endpoints(self):
        for url in (
            "https://www.binance.com/bapi/futures/v1/private/future/copy-trade/copy-portfolio/create",
            "https://www.binance.com/bapi/asset/v1/private/asset-service/wallet/transfer",
            BALANCE_URL + "?token=secret",
            "https://evil.test/balance",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                BalanceQuery(url, "POST", {})


class PolicyTests(unittest.TestCase):
    def test_fixed_and_minimum_allocations_use_exact_decimals(self):
        policy = JoinPolicy("1000", "400")
        self.assertEqual(policy.amount(snapshot()), Decimal("300"))
        fixed = JoinPolicy("1000", "400", allocation="FIXED", fixed_amount="350.12345678")
        self.assertEqual(fixed.payload(snapshot())["investAmount"], "350.12345678")
        self.assertEqual(fixed.payload(snapshot())["copyModel"], "FIXED_RATIO")

    def test_fixed_amount_mode_uses_its_own_minimum_and_per_order_amount(self):
        policy = JoinPolicy("1000", "400", "FIXED_AMT", cost_per_order="10")
        self.assertEqual(policy.amount(snapshot()), Decimal("400"))
        self.assertEqual(policy.payload(snapshot())["costPerOrder"], "10")
        self.assertIsNone(policy.amount(snapshot(minimum_fixed=None)))

    def test_invalid_money_modes_and_risk_are_rejected(self):
        for changes in ({"total_budget": "NaN"}, {"per_target_cap": "-1"}, {"copy_model": "BAD"},
                        {"allocation": "FIXED", "fixed_amount": "500"}, {"leverage_mode": "FIX_LEVERAGE", "leverage_value": 11},
                        {"stop_loss": "4"}, {"total_stop_loss": "96"}, {"max_attempts": 0}):
            args = {"total_budget": "1000", "per_target_cap": "400"} | changes
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                JoinPolicy(**args)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "joins.sqlite3"
        self.journal = Journal(self.path, "test-owner")
        self.policy = JoinPolicy("600", "300")

    def tearDown(self):
        self.temp.cleanup()

    def test_crash_record_survives_restart_and_blocks_duplicate(self):
        self.journal.reserve(TARGET, Decimal("300"), self.policy, {"test": "payload"})
        reopened = Journal(self.path, "test-owner")
        self.assertEqual(reopened.get(TARGET)["state"], "SENDING")
        with self.assertRaises(RequestFailure):
            reopened.reserve(TARGET, Decimal("300"), self.policy, {})

    def test_old_operation_cannot_overwrite_new_reservation(self):
        old = self.journal.reserve(TARGET, Decimal("300"), self.policy, {})
        self.journal.transition(TARGET, "REJECTED", operation=old)
        new = self.journal.reserve(TARGET, Decimal("300"), self.policy, {})
        with self.assertRaises(RequestFailure):
            self.journal.transition(TARGET, "REJECTED", operation=old)
        self.assertEqual(self.journal.get(TARGET)["operation_id"], new)
        self.assertEqual(self.journal.get(TARGET)["state"], "SENDING")

    def test_other_session_cannot_reconcile_or_unlock_records(self):
        self.journal.reserve(TARGET, Decimal("300"), self.policy, {})
        other = Journal(self.path, "different-session")
        with self.assertRaises(RequestFailure):
            other.transition(TARGET, "JOINED", copy_id=COPY_ID)
        with self.assertRaises(RequestFailure):
            other.release_after_manual_check(TARGET)
        self.assertEqual(self.journal.get(TARGET)["state"], "SENDING")

    def test_automatic_run_and_manual_recovery_exclude_other_processes(self):
        with self.journal.exclusive():
            other_lock = Journal(self.path, "test-owner").exclusive()
            try:
                with self.assertRaises(RequestFailure):
                    other_lock.acquire()
            finally:
                other_lock.release()
        with self.journal.exclusive():
            pass

    def test_budget_includes_success_and_unknown_outcomes(self):
        self.journal.reserve(TARGET, Decimal("300"), self.policy, {})
        self.journal.transition(TARGET, "JOINED", copy_id=COPY_ID)
        self.journal.reserve(OTHER, Decimal("300"), self.policy, {})
        with self.assertRaises(RequestFailure):
            self.journal.reserve("5075281354358777858", Decimal("1"), self.policy, {})

    def test_explicit_rejection_releases_money_but_preserves_attempt_count(self):
        policy = JoinPolicy("300", "300", max_attempts=1)
        self.journal.reserve(TARGET, Decimal("300"), policy, {})
        self.journal.transition(TARGET, "REJECTED")
        with self.assertRaises(RequestFailure):
            self.journal.reserve(TARGET, Decimal("300"), policy, {})
        self.journal.reserve(OTHER, Decimal("300"), policy, {})

    def test_two_process_like_instances_cannot_double_reserve(self):
        def reserve():
            try:
                Journal(self.path, "test-owner").reserve(TARGET, Decimal("300"), self.policy, {})
                return True
            except RequestFailure:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reserve(), range(2)))
        self.assertEqual(sum(results), 1)


class FakeClient:
    def __init__(self):
        self.joined = False
        self.creates = []
        self.result = None
        self.balance_value = Decimal("600")
        self.details = 0
        self.full_on_recheck = False

    def balance(self):
        return self.balance_value

    def detail(self, target):
        self.details += 1
        value = snapshot(1000 if self.full_on_recheck and self.details > 1 else 999, portfolio_id=target)
        return AccountDetail(value, self.joined, COPY_ID if self.joined else None)

    def create(self, payload):
        self.creates.append(payload)
        if self.result:
            raise self.result
        self.joined = True


class AutoJoinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "joins.sqlite3", "test-owner")
        self.client = FakeClient()
        self.events = []
        self.stop = threading.Event()
        self.joiner = AutoJoiner(self.client, JoinPolicy("600", "300"), self.journal, self.events.append)

    def tearDown(self):
        self.joiner.close()
        self.temp.cleanup()

    def preflight(self):
        self.joiner.preflight((TARGET,), self.stop)

    def test_normal_flow_confirms_with_account_detail_and_never_double_submits(self):
        self.preflight()
        self.joiner.step(snapshot(), self.stop)
        self.joiner.step(snapshot(), self.stop)
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")
        self.assertIn("joined", [event.kind for event in self.events])
        receipts = [event.confirmation for event in self.events if event.kind == "joined"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].copy_id, COPY_ID)
        self.assertEqual(receipts[0].operation_id, self.journal.get(TARGET)["operation_id"])
        self.assertEqual(receipts[0].amount, Decimal(self.client.creates[0]["investAmount"]))

    def test_success_receipt_uses_actual_fixed_investment_not_live_minimum(self):
        self.joiner.policy = JoinPolicy("600", "400", allocation="FIXED", fixed_amount="350.12345678")
        self.preflight()
        self.joiner.step(snapshot(), self.stop)
        success = next(event for event in self.events if event.kind == "joined")
        self.assertEqual(success.snapshot.minimum, Decimal("300"))
        self.assertEqual(success.confirmation.amount, Decimal("350.12345678"))
        self.assertEqual(success.confirmation.copy_model, "FIXED_RATIO")

    def test_normal_monitoring_full_slots_does_not_use_submission_attempts(self):
        self.joiner.policy = JoinPolicy("600", "300", max_attempts=1)
        self.preflight()
        for _ in range(150):
            self.joiner.step(snapshot(1000), self.stop)
        self.assertFalse(self.client.creates)
        self.assertIsNone(self.journal.get(TARGET))
        self.joiner.step(snapshot(), self.stop)
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")

    def test_existing_join_skips_creation(self):
        self.client.joined = True
        self.preflight()
        self.joiner.step(snapshot(), self.stop)
        self.assertFalse(self.client.creates)
        self.assertFalse(any(event.confirmation or event.kind == "joined" for event in self.events))

    def test_slot_lost_during_recheck_never_submits(self):
        self.preflight()
        self.client.full_on_recheck = True
        self.joiner.step(snapshot(), self.stop)
        self.assertFalse(self.client.creates)

    def test_balance_failure_or_insufficient_funds_never_submits(self):
        self.preflight()
        self.client.balance_value = Decimal("299")
        with self.assertRaises(RequestFailure):
            self.joiner.step(snapshot(), self.stop)
        self.assertFalse(self.client.creates)

    def test_timeout_then_confirmed_join_does_not_repeat_post(self):
        self.preflight()
        def submit(payload):
            self.client.creates.append(payload)
            self.client.joined = True
            raise JoinUncertain()
        self.client.create = submit
        self.joiner.step(snapshot(), self.stop)
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.journal.get(TARGET)["state"], "JOINED")

    def test_unconfirmed_timeout_resumes_readonly_after_restart(self):
        self.preflight()
        self.client.result = JoinUncertain()
        class NoWait:
            def wait(self, _): return False
            def is_set(self): return False
        self.joiner.step(snapshot(), NoWait())
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.journal.get(TARGET)["state"], "VERIFYING")
        self.assertFalse(any(event.confirmation or event.kind == "joined" for event in self.events))
        self.joiner.close()
        self.assertEqual(self.journal.get(TARGET)["state"], "UNCERTAIN")
        restarted = AutoJoiner(self.client, JoinPolicy("600", "300"), self.journal, self.events.append)
        try:
            restarted.preflight((TARGET,), self.stop)
            self.assertIn(TARGET, restarted.pending)
            restarted.step(snapshot(), self.stop)
        finally:
            restarted.close()
        self.assertEqual(len(self.client.creates), 1)

    def test_explicit_full_rejection_is_only_automatic_retry_case(self):
        self.joiner.policy = JoinPolicy("600", "300", direct_retry=True)
        self.preflight()
        self.client.result = JoinRejected("11012017")
        self.joiner.step(snapshot(1000), self.stop)
        self.joiner.step(snapshot(1000), self.stop)
        self.assertEqual(len(self.client.creates), 2)
        self.assertEqual(self.journal.get(TARGET)["state"], "REJECTED")

    def test_other_rejection_stops_and_cancel_does_not_post(self):
        self.preflight()
        self.client.result = JoinRejected("100001")
        with self.assertRaises(JoinRejected):
            self.joiner.step(snapshot(), self.stop)
        self.stop.set()
        self.joiner.step(snapshot(), self.stop)
        self.assertEqual(len(self.client.creates), 1)

    def test_stale_or_unknown_amount_never_posts(self):
        self.preflight()
        with self.assertRaises(RequestFailure):
            self.joiner.step(snapshot(checked_at=time.time()-20), self.stop)
        self.joiner.step(snapshot(minimum=None), self.stop)
        self.assertFalse(self.client.creates)


class PrivateHTTPTests(unittest.TestCase):
    class Response(io.BytesIO):
        status = 200
        def __init__(self, data, url):
            super().__init__(json.dumps(data).encode())
            self.url = url
        def geturl(self): return self.url

    class Opener:
        def __init__(self, response): self.response = response; self.calls = []
        def open(self, request, timeout):
            self.calls.append(request)
            if isinstance(self.response, Exception): raise self.response
            return self.response

    def client(self, opener):
        return PrivateClient(Session({"cookie": "local-only", "csrftoken": "local"}), BalanceQuery(BALANCE_URL), opener=opener)

    def test_post_is_json_and_never_retries_in_transport(self):
        opener = self.Opener(TimeoutError())
        with self.assertRaises(JoinUncertain):
            self.client(opener).create({"leadPortfolioId": TARGET, "investAmount": "300"})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0].method, "POST")
        self.assertEqual(json.loads(opener.calls[0].data)["investAmount"], "300")

    def test_known_full_code_accepts_numeric_or_missing_success_but_not_contradictions(self):
        url = "https://www.binance.com/bapi/futures/v1/private/future/copy-trade/copy-portfolio/create"
        for payload in ({"code": 11012017, "success": False}, {"code": "11012017"}):
            with self.subTest(payload=payload), self.assertRaises(JoinRejected) as raised:
                self.client(self.Opener(self.Response(payload, url))).create({})
            self.assertEqual(raised.exception.code, "11012017")
        for payload in ({"code": "11012017", "success": True}, {"code": "11012017", "success": "true"},
                        {"code": "11012017", "success": None}, {"code": "123456"},
                        {"code": "private-secret", "message": "private-message"}):
            with self.subTest(payload=payload), self.assertRaises(JoinUncertain) as raised:
                self.client(self.Opener(self.Response(payload, url))).create({})
            self.assertNotIn("private-secret", str(raised.exception))
            self.assertNotIn("private-message", str(raised.exception))

    def test_http_create_diagnostic_retains_status_and_cooldown_without_raw_response(self):
        error = urllib.error.HTTPError(BALANCE_URL, 429, "private-secret", {"Retry-After": "90"}, io.BytesIO(b"private-response"))
        opener = self.Opener(error)
        with self.assertRaises(JoinUncertain) as raised:
            self.client(opener).create({})
        self.assertIn("HTTP 429", str(raised.exception))
        self.assertEqual(raised.exception.retry_after, 90)
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_spot_v3_balance_keeps_request_and_reads_only_free_usdt(self):
        # Synthetic response shape; no real account response is stored in tests.
        body = {"needBtcValuation": True, "quoteAsset": "USDT"}
        payload = {"code": "000000", "success": True, "data": [
            {"asset": "BTC", "free": "0.01"},
            {"asset": "USDT", "free": "301.23456789", "locked": "75", "quoteAssetValuation": "376.23456789"},
        ]}
        opener = self.Opener(self.Response(payload, SPOT_ASSET_V3_URL))
        client = PrivateClient(Session({"cookie": "synthetic", "csrftoken": "synthetic"}),
                               BalanceQuery(SPOT_ASSET_V3_URL, "POST", body), opener=opener)
        self.assertEqual(client.balance(), Decimal("301.23456789"))
        self.assertEqual(len(opener.calls), 1)
        request = opener.calls[0]
        self.assertEqual(request.full_url, SPOT_ASSET_V3_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), body)

    def test_redirects_are_blocked_before_credentials_can_follow(self):
        with self.assertRaises(RequestFailure):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test")

    def test_balance_guest_response_or_html_does_not_pass_preflight(self):
        response = self.Response({"code": "100001", "success": False}, BALANCE_URL)
        with self.assertRaises(RequestFailure):
            self.client(self.Opener(response)).balance()

    def test_401_suggests_refreshing_the_single_wallet_request_without_retry(self):
        error = urllib.error.HTTPError(BALANCE_URL, 401, "Unauthorized", {}, io.BytesIO(b"private-response"))
        opener = self.Opener(error)
        with self.assertRaises(RequestFailure) as raised:
            self.client(opener).balance()
        self.assertTrue(raised.exception.fatal)
        self.assertIn("401", str(raised.exception))
        self.assertIn("最新余额请求", str(raised.exception))
        self.assertNotIn("private-response", str(raised.exception))
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()

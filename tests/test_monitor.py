from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from binance_monitor.core import (
    DemoClient, Monitor, PublicClient, RequestFailure, Settings, Snapshot, ValidationError,
    detect_local_proxy, parse_snapshot, parse_targets, portfolio_id, retry_after_seconds, validate_proxy,
)
from binance_monitor.storage import load_config, save_config


TARGET = "5075281354358777856"
OTHER = "5075281354358777857"


def payload(**changes):
    data = {"leadPortfolioId": TARGET, "nickname": "示例", "currentCopyCount": 1000,
            "maxCopyCount": 1000, "fixedRadioMinCopyUsd": "300", "status": "ACTIVE",
            "isPaused": False, "portfolioType": "PUBLIC"}
    data.update(changes)
    return {"code": "000000", "success": True, "data": data}


def snapshot(count=1000, *, minimum=Decimal("300"), status="ACTIVE", target=TARGET):
    return Snapshot(target, "示例", count, 1000, minimum, status, 100.0)


class ControlledStop:
    def __init__(self, requests):
        self.requests = requests
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = len(self.waits) >= self.requests
        return self.stopped


class SequenceClient:
    def __init__(self, values):
        self.values = iter(values)
        self.targets = []

    def fetch(self, target):
        self.targets.append(target)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def run_sequence(values, *, targets=(TARGET,), interval=1):
    events = []
    stop = ControlledStop(len(values))
    client = SequenceClient(values)
    monitor = Monitor(Settings(targets, interval=interval), events.append,
                      client=client, stop=stop, clock=lambda: 0)
    monitor.run()
    return events, stop, client


class ValidationTests(unittest.TestCase):
    def test_detects_only_local_system_proxies_without_credentials(self):
        for proxies, expected in (({"https": "http://127.0.0.1:7890"}, "http://127.0.0.1:7890"),
                                  ({"https": "http://user:secret@127.0.0.1:7890"}, ""),
                                  ({"https": "http://remote.example:8080"}, "")):
            with self.subTest(proxies=proxies), patch("urllib.request.getproxies", return_value=proxies):
                self.assertEqual(detect_local_proxy(), expected)

    def test_official_chinese_url_and_markdown_keep_numeric_id_exact(self):
        url = f"https://www.binance.com/zh-CN/copy-trading/lead-details/{TARGET}?timeRange=30D&isSmartFilter=true"
        self.assertEqual(portfolio_id(url), TARGET)
        self.assertEqual(portfolio_id(f"[带单员]({url})"), TARGET)
        self.assertEqual(parse_targets(f"{TARGET}\n{url}\n{OTHER}"), (TARGET, OTHER))

    def test_untrusted_domains_credentials_and_schemes_rejected(self):
        for url in (
            f"https://www.binance.com.evil.test/copy-trading/lead-details/{TARGET}",
            f"https://evil.test@www.binance.com/copy-trading/lead-details/{TARGET}",
            f"http://www.binance.com/copy-trading/lead-details/{TARGET}",
            f"https://www.binance.com:444/copy-trading/lead-details/{TARGET}",
            "https://www.binance.com/no-detail-page",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                portfolio_id(url)

    def test_reject_unbounded_polling_and_nonfinite_amounts(self):
        self.assertEqual(Settings((TARGET,), interval=0.1).interval, 0.1)
        for interval in (0.099, 0, float("nan"), float("inf"), 301):
            with self.subTest(interval=interval), self.assertRaises(ValidationError):
                Settings((TARGET,), interval=interval)
        for amount in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal(0)):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                Settings((TARGET,), ceiling=amount)

    def test_proxy_passwords_are_not_accepted_for_persistence(self):
        self.assertEqual(validate_proxy("http://127.0.0.1:7890"), "http://127.0.0.1:7890")
        for proxy in ("http://user:secret@localhost:7890", "socks5://localhost:1080", "http://localhost", "http://localhost:7890/path"):
            with self.subTest(proxy=proxy), self.assertRaises(ValidationError):
                validate_proxy(proxy)


class SchemaTests(unittest.TestCase):
    def test_reduced_real_public_response_fixture(self):
        fixture = Path(__file__).parent / "fixtures" / "public_detail.json"
        value = parse_snapshot(json.loads(fixture.read_text(encoding="utf-8")), TARGET)
        self.assertEqual((value.nickname, value.copiers, value.capacity, value.minimum),
                         ("熬鹰资本", 1000, 1000, Decimal("300.0")))
        self.assertEqual(value.state(Decimal("300")), "名额已满")

    def test_paused_private_or_unknown_type_never_alerts(self):
        for changes in ({"isPaused": True}, {"isPaused": None}, {"portfolioType": "PRIVATE"}, {"portfolioType": ""}):
            with self.subTest(changes=changes):
                value = parse_snapshot(payload(currentCopyCount=999, **changes), TARGET)
                self.assertNotEqual(value.state(Decimal("300")), "发现空位")

    def test_effective_or_risk_limit_conservatively_caps_slots(self):
        for changes in ({"finalEffectiveMaxCopyCount": 900}, {"riskControlMaxCopyCount": 900},
                        {"finalEffectiveMaxCopyCount": 950, "riskControlMaxCopyCount": 900}):
            with self.subTest(changes=changes):
                value = parse_snapshot(payload(currentCopyCount=999, **changes), TARGET)
                self.assertEqual(value.slots, 0)
                self.assertEqual(value.state(Decimal("300")), "名额已满")

    def test_full_and_open_capacity(self):
        full = parse_snapshot(payload(), TARGET)
        self.assertEqual(full.state(Decimal("300")), "名额已满")
        opened = parse_snapshot(payload(currentCopyCount="999"), TARGET)
        self.assertEqual(opened.slots, 1)
        self.assertEqual(opened.state(Decimal("300")), "发现空位")

    def test_missing_invalid_capacity_never_means_available(self):
        for changes in (
            {"maxCopyCount": None}, {"maxCopyCount": 0}, {"currentCopyCount": -1},
            {"currentCopyCount": True}, {"currentCopyCount": 999.5},
        ):
            with self.subTest(changes=changes), self.assertRaises(RequestFailure):
                parse_snapshot(payload(**changes), TARGET)

    def test_unknown_state_or_minimum_suppresses_alert(self):
        for changes, expected in (
            ({"status": ""}, "状态待确认"),
            ({"status": "CLOSED"}, "暂不可跟单"),
            ({"fixedRadioMinCopyUsd": None}, "金额未知"),
            ({"fixedRadioMinCopyUsd": "300.01"}, "最低金额超限"),
        ):
            with self.subTest(changes=changes):
                value = parse_snapshot(payload(currentCopyCount=999, **changes), TARGET)
                self.assertEqual(value.state(Decimal("300")), expected)

    def test_business_failure_null_detail_and_wrong_id_fail_closed(self):
        for data in ({"code": "11012017", "data": {}}, {"code": "000000", "data": None},
                     payload(leadPortfolioId=OTHER), [], payload(fixedRadioMinCopyUsd="NaN")):
            with self.subTest(data=data), self.assertRaises(RequestFailure):
                parse_snapshot(data, TARGET)


class MonitorTests(unittest.TestCase):
    def test_full_to_open_alert_once_then_rearm_only_after_unavailable(self):
        events, _, _ = run_sequence([snapshot(), snapshot(999), snapshot(999), snapshot(), snapshot(999)])
        self.assertEqual(sum(e.kind == "available" for e in events), 2)

    def test_open_at_start_alerts_immediately(self):
        events, _, _ = run_sequence([snapshot(999)])
        self.assertEqual([e.kind for e in events], ["checking", "snapshot", "available", "stopped"])

    def test_no_duplicate_alert_after_network_error(self):
        events, _, _ = run_sequence([snapshot(999), RequestFailure("网络失败"), snapshot(999)])
        self.assertEqual(sum(e.kind == "available" for e in events), 1)

    def test_amount_filter_and_unknown_status(self):
        events, _, _ = run_sequence([
            snapshot(999, minimum=None), snapshot(999, status=""),
            snapshot(999, minimum=Decimal("301")), snapshot(999),
        ])
        self.assertEqual(sum(e.kind == "available" for e in events), 1)

    def test_global_request_rate_is_shared_across_targets(self):
        _, stop, client = run_sequence([snapshot(), snapshot(target=OTHER)], targets=(TARGET, OTHER), interval=2)
        self.assertEqual(client.targets, [TARGET, OTHER])
        self.assertEqual(stop.waits, [2, 2])

    def test_rate_limit_cools_all_targets_before_next_request(self):
        events, stop, client = run_sequence([RequestFailure("限流", retry_after=90), snapshot(target=OTHER)], targets=(TARGET, OTHER))
        self.assertEqual(stop.waits, [90, 1])
        self.assertEqual(client.targets, [TARGET, OTHER])
        self.assertEqual(sum(e.kind == "cooldown" for e in events), 1)

    def test_retry_deadline_is_measured_from_response_not_request_start(self):
        times = iter([0, 8, 8])
        stop = ControlledStop(1)
        Monitor(Settings((TARGET,)), lambda _: None,
                client=SequenceClient([RequestFailure("限流", retry_after=90)]),
                stop=stop, clock=lambda: next(times)).run()
        self.assertEqual(stop.waits, [90])

    def test_block_or_schema_change_stops_before_other_targets(self):
        events, stop, client = run_sequence([RequestFailure("封禁", fatal=True), snapshot(target=OTHER)], targets=(TARGET, OTHER))
        self.assertEqual(client.targets, [TARGET])
        self.assertFalse(stop.waits)
        self.assertEqual([e.kind for e in events][-2:], ["halted", "stopped"])

    def test_cancel_during_inflight_request_cannot_emit_late_alert(self):
        stop = threading.Event()
        class CancellingClient:
            def fetch(self, _target):
                stop.set()
                return snapshot(999)
        events = []
        Monitor(Settings((TARGET,)), events.append, stop=stop, client=CancellingClient()).run()
        self.assertEqual([e.kind for e in events], ["checking", "stopped"])

    def test_unexpected_internal_error_stops_and_reports(self):
        events, _, client = run_sequence([RuntimeError("private details"), snapshot()])
        self.assertEqual(len(client.targets), 1)
        self.assertEqual([e.kind for e in events][-3:], ["error", "halted", "stopped"])
        self.assertNotIn("private details", "".join(e.message for e in events))


class HTTPTests(unittest.TestCase):
    class Response(io.BytesIO):
        def geturl(self):
            return "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail"

    class Opener:
        def __init__(self, value):
            self.value = value

        def open(self, request, timeout):
            self.request = request
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    def test_request_uses_only_public_get_and_no_credentials(self):
        opener = self.Opener(self.Response(json.dumps(payload()).encode()))
        value = PublicClient(opener=opener).fetch(TARGET)
        self.assertEqual(value.copiers, 1000)
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertNotIn("Cookie", dict(opener.request.header_items()))
        self.assertIn(TARGET, opener.request.full_url)

    def test_http_429_respects_retry_after(self):
        error = urllib.error.HTTPError("https://www.binance.com", 429, "limit", {"Retry-After": "120"}, None)
        with self.assertRaises(RequestFailure) as caught:
            PublicClient(opener=self.Opener(error)).fetch(TARGET)
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertFalse(caught.exception.fatal)

    def test_block_http_codes_are_fatal(self):
        for code in (401, 403, 418, 451, 404, 410):
            with self.subTest(code=code), self.assertRaises(RequestFailure) as caught:
                error = urllib.error.HTTPError("https://www.binance.com", code, "blocked", {}, None)
                PublicClient(opener=self.Opener(error)).fetch(TARGET)
            self.assertTrue(caught.exception.fatal)

    def test_challenge_html_is_not_a_slot(self):
        with self.assertRaises(RequestFailure) as caught:
            PublicClient(opener=self.Opener(self.Response(b"<html>verification</html>"))).fetch(TARGET)
        self.assertTrue(caught.exception.fatal)

    def test_retry_after_handles_dates_bad_values_and_long_deadline(self):
        self.assertEqual(retry_after_seconds("Wed, 11 Nov 2026 00:02:00 GMT", now=1794355200), 120)
        for value in (None, "NaN", "bad"):
            self.assertEqual(retry_after_seconds(value), 60)
        self.assertEqual(retry_after_seconds("7200"), 7200)


class StorageTests(unittest.TestCase):
    def test_config_roundtrip_is_atomic_and_does_not_persist_demo_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(load_config(path), {})
            save_config({"targets": [TARGET], "ceiling": "300"}, path)
            self.assertEqual(load_config(path)["targets"], [TARGET])
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_demo_has_no_http_and_exercises_full_to_open(self):
        client = DemoClient()
        self.assertEqual([client.fetch(TARGET).slots for _ in range(3)], [0, 0, 1])


if __name__ == "__main__":
    unittest.main()

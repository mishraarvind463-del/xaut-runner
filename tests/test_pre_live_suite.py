#!/usr/bin/env python3
"""Offline XAUTUSD pre-live regression tests; never contacts Delta or sends email."""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch


def load_bot(source: pathlib.Path):
    if not source.is_file():
        raise RuntimeError(f"Bot source not found: {source}")
    spec = importlib.util.spec_from_file_location("xaut_pre_live_test_bot", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import bot source: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python tests/test_pre_live_suite.py private-bot/bot.py", file=sys.stderr)
        return 2

    os.environ.update({
        "API_KEY": "offline-test-key",
        "API_SECRET": "offline-test-secret",
        "ENABLE_LIVE_TRADING": "false",
        "BOT_RUNTIME_MINUTES": "5",
    })
    bot = load_bot(pathlib.Path(sys.argv[1]).resolve())
    passed: list[str] = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            raise AssertionError(name)
        passed.append(name)
        print(f"PASS: {name}")

    # Strategy identity and default safety configuration.
    check("symbol/product/candle source", bot.SYMBOL == "XAUTUSD" and bot.PRODUCT_ID == 131253 and bot.CANDLE_SYMBOL == "MARK:XAUTUSD")
    check("1m timeframe and 3-second close delay", bot.CANDLE_CLOSE_DELAY == 3)
    check("user-approved temporary Supertrend settings", bot.ATR_PERIOD == 2 and bot.MULTIPLIER == 1.0)
    check("Paper Mode is default and Live is disabled", bot.PAPER_MODE is True and bot.LIVE_TRADING is False)

    # Confirmation guard must reject a Live request without the exact second factor.
    old_request, old_valid = bot.LIVE_TRADING_REQUESTED, bot.LIVE_TRADING_CONFIRM_VALID
    try:
        bot.LIVE_TRADING_REQUESTED = True
        bot.LIVE_TRADING_CONFIRM_VALID = False
        rejected = False
        try:
            bot.validate_configuration()
        except ValueError:
            rejected = True
        check("incorrect Live confirmation is rejected", rejected)
    finally:
        bot.LIVE_TRADING_REQUESTED, bot.LIVE_TRADING_CONFIRM_VALID = old_request, old_valid

    # Deterministic Supertrend direction fixtures.
    rising, falling = [], []
    for index in range(20):
        close = 100.0 + index * 1.5
        rising.append({"high": close + 1.0, "low": close - 1.0, "close": close + 0.5})
        close = 130.0 - index * 1.5
        falling.append({"high": close + 1.0, "low": close - 1.0, "close": close - 0.5})
    check("rising candle fixture produces BUY", bot.calculate_supertrend(rising)[0] == 1)
    check("falling candle fixture produces SELL", bot.calculate_supertrend(falling)[0] == -1)

    # Paper position persistence, idempotent CSV outbox and a fresh manifest.
    previous_cwd = pathlib.Path.cwd()
    with tempfile.TemporaryDirectory(prefix="xaut-pre-live-paper-") as temp_dir:
        os.chdir(temp_dir)
        try:
            bot.PAPER_MODE = True
            bot.ALERTS = None
            state = bot.default_paper_state()
            check("Paper BUY entry persisted", bot.paper_open(state, "buy", 100.0) and state["position"] == 1)
            check("Paper exit and CSV outbox reconciled", bot.paper_close(state, 102.5, "TEST_REVERSAL") and state["position"] == 0)
            payload = json.loads(pathlib.Path("paper_state.json").read_text(encoding="utf-8"))
            check("Paper state identity and flat state verified", payload["symbol"] == "XAUTUSD" and payload["product_id"] == 131253 and payload["state"]["position"] == 0)
            with open("trade_history.csv", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            check("exactly one completed Paper trade in CSV", len(rows) == 1 and rows[0]["exit_reason"] == "TEST_REVERSAL")
            manifest = bot.write_state_manifest()
            check("fresh PAPER manifest created", manifest["mode"] == "PAPER" and manifest["symbol"] == "XAUTUSD" and "paper_state.json" in manifest["files"])
        finally:
            os.chdir(previous_cwd)

    # Cooperative stop handling and Paper shutdown must preserve an open Paper position.
    import signal
    bot.STOP_REQUESTED.clear()
    bot._handle_shutdown_signal(signal.SIGTERM, None)
    check("SIGTERM requests a cooperative stop", bot.stop_requested() is True)
    check("cooperative wait exits when stop is requested", bot.wait_or_stop(0.01) is True)
    bot.STOP_REQUESTED.clear()
    with tempfile.TemporaryDirectory(prefix="xaut-pre-live-shutdown-") as temp_dir:
        previous_cwd = pathlib.Path.cwd()
        os.chdir(temp_dir)
        try:
            preserved = bot.default_paper_state()
            preserved.update({"position": 1, "entry_price": 3800.0, "entry_time": "2026-11-16T00:00:00+00:00", "entry_side": "buy"})
            check("Paper shutdown succeeds while preserving open position", bot.shutdown_bot(preserved, None, None) is True and preserved["position"] == 1)
            saved = json.loads(pathlib.Path("paper_state.json").read_text(encoding="utf-8"))
            check("open Paper position survives shutdown persistence", saved["state"]["position"] == 1)
        finally:
            os.chdir(previous_cwd)

    # Paper Mode must reject any authenticated write before HTTP is called.
    bot.PAPER_MODE = True
    with patch.object(bot.requests, "request") as mocked_request:
        denied = False
        try:
            bot.req("POST", "/v2/orders", {})
        except RuntimeError:
            denied = True
        check("Paper Mode blocks order POST", denied and mocked_request.call_count == 0)

    # Network errors: bounded retry for reads; no blind replay of writes.
    bot.PAPER_MODE = False
    bot.API_KEY, bot.API_SECRET = "offline-test-key", "offline-test-secret"
    calls = {"GET": 0, "POST": 0, "DELETE": 0}

    def simulated_timeout(method, *args, **kwargs):
        calls[method] += 1
        raise bot.requests.Timeout("simulated offline timeout")

    with patch.object(bot.requests, "request", side_effect=simulated_timeout), patch.object(bot.time, "sleep", return_value=None):
        get_failed = False
        try:
            bot.req("GET", "/v2/positions")
        except bot.APIError:
            get_failed = True
        check("GET retries are bounded at configured maximum", get_failed and calls["GET"] == bot.MAX_API_READ_ATTEMPTS)
        for method in ("POST", "DELETE"):
            try:
                bot.req(method, "/v2/orders", {})
            except bot.APIError:
                pass
            else:
                raise AssertionError(f"{method} timeout did not raise APIError")
    check("ambiguous POST is attempted once only", calls["POST"] == 1)
    check("ambiguous DELETE is attempted once only", calls["DELETE"] == 1)
    bot.PAPER_MODE = True

    check("Delta rate-limit reset header honored", abs(bot._retry_delay(1, {"X-RATE-LIMIT-RESET": "2500"}, 429) - 2.5) < 1e-9)
    check("429 missing reset uses conservative fallback", bot._retry_delay(1, {}, 429) == bot.RATE_LIMIT_FALLBACK_WAIT_SECONDS)

    # SMTP is mocked: this test never sends an actual email.
    class FakeSMTP:
        sent: list[str] = []
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ehlo(self): pass
        def starttls(self, **kwargs): pass
        def login(self, *args): pass
        def send_message(self, message): self.sent.append(str(message["Subject"]))

    with tempfile.TemporaryDirectory(prefix="xaut-pre-live-alerts-") as temp_dir:
        os.chdir(temp_dir)
        try:
            bot.ALERT_STATE_FILE = "alert_state.json"
            pathlib.Path("alert_state.json").write_text(json.dumps({
                "version": 1,
                "active_incidents": {},
                "queue": [
                    {"id": "old-start", "subject": "XAUT BOT BOT STARTED: XAUTUSD", "body": "legacy"},
                    {"id": "old-stop", "subject": "XAUT BOT BOT STOPPED: XAUTUSD", "body": "legacy"},
                    {"id": "old-recovery", "subject": "XAUT BOT RECOVERED: XAUTUSD", "body": "legacy"},
                    {"id": "trade", "subject": "XAUT BOT ENTRY CONFIRMED: XAUTUSD", "body": "entry"},
                    {"id": "failure", "subject": "XAUT BOT FAILURE: PREVIOUS_ERROR", "body": "failure"},
                ],
            }), encoding="utf-8")
            os.environ.update({
                "SMTP_HOST": "smtp.invalid", "SMTP_PORT": "587", "SMTP_USER": "offline-user",
                "SMTP_PASSWORD": "offline-password", "ALERT_EMAIL_TO": "offline-recipient",
                "ALERT_EMAIL_FROM": "offline-sender", "SMTP_USE_SSL": "false",
            })
            with patch.object(bot.smtplib, "SMTP", FakeSMTP):
                alerts = bot.AlertManager()
                check("legacy startup/shutdown/recovery queue items discarded", len(alerts.data["queue"]) == 2)
                alerts.flush()
                check("only approved entry and failure emails flushed", set(FakeSMTP.sent) == {
                    "XAUT BOT ENTRY CONFIRMED: XAUTUSD", "XAUT BOT FAILURE: PREVIOUS_ERROR"
                })
                alerts.incident("TEST_FAILURE", "simulated failure")
                alerts.incident("TEST_FAILURE", "duplicate failure")
                alerts.recovered("TEST_FAILURE", "silent recovery")
                check("one failure email per incident; recovery is silent", FakeSMTP.sent.count("XAUT BOT FAILURE: TEST_FAILURE") == 1)
                before = len(FakeSMTP.sent)
                alerts.notify("BOT STARTED", "must not send")
                check("non-trade notification is suppressed", len(FakeSMTP.sent) == before)
                alerts.notify("EXIT CONFIRMED", "confirmed exit")
                check("confirmed exit notification is sent", FakeSMTP.sent[-1] == "XAUT BOT EXIT CONFIRMED: XAUTUSD")
        finally:
            os.chdir(previous_cwd)

    print(f"COMBINED PRE-LIVE REGRESSION SUITE: ALL {len(passed)} CHECKS PASSED (offline; no Delta API calls; no real email).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"COMBINED PRE-LIVE REGRESSION SUITE: FAILED: {error!r}", file=sys.stderr)
        raise SystemExit(1)

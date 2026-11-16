#!/usr/bin/env python3
"""Offline mock tests of XAUTUSD Live execution safeguards. No Delta calls/orders."""
import importlib.util
import json
import os
import pathlib
import tempfile
import sys
from unittest.mock import patch

BOT_PATH = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).with_name('bot.py').resolve()
os.environ.update({
    'API_KEY': 'offline-mock-key',
    'API_SECRET': 'offline-mock-secret',
    'ENABLE_LIVE_TRADING': 'true',
    'LIVE_TRADING_CONFIRM': 'CONFIRM_XAUTUSD_131253',
})
spec = importlib.util.spec_from_file_location('xaut_live_mock_bot', BOT_PATH)
bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)

bot.API_KEY = 'offline-mock-key'
bot.API_SECRET = 'offline-mock-secret'
bot.PAPER_MODE = False
check_live_config = bot.LIVE_TRADING is True and bot.PAPER_MODE is False
if not check_live_config:
    raise AssertionError('mock suite did not enter explicit test-only Live configuration')

passed = []
def check(label, condition):
    if not condition:
        raise AssertionError(label)
    passed.append(label)
    print('PASS:', label)

class FakeAlerts:
    def notify(self, *args, **kwargs): pass

bot.ALERTS = FakeAlerts()
bot.alert_incident = lambda *a, **k: None
bot.alert_recovered = lambda *a, **k: None

with tempfile.TemporaryDirectory(prefix='xaut-live-mock-') as tmp:
    old_cwd = pathlib.Path.cwd()
    os.chdir(tmp)
    try:
        # 1. Simulated successful live BUY order; intent must be durable before POST.
        journal = bot.default_execution_journal()
        order_result = {
            'id': 8123, 'product_id': bot.PRODUCT_ID, 'client_order_id': None,
            'side': 'buy', 'size': 1, 'unfilled_size': 0,
            'order_type': 'market_order', 'reduce_only': False,
            'state': 'closed', 'average_fill_price': '3800.0'
        }
        post_payloads = []
        persisted_before_post = []
        def fake_req(method, path, payload=None):
            if method == 'POST' and path == '/v2/orders':
                data = json.loads(pathlib.Path(bot.EXECUTION_JOURNAL_FILE).read_text())
                persisted_before_post.append(data['pending_order']['phase'] == 'prepared_before_post')
                order_result['client_order_id'] = payload['client_order_id']
                post_payloads.append(dict(payload))
                return {'success': True, 'result': dict(order_result)}
            if method == 'GET' and path == '/v2/orders/8123':
                return {'success': True, 'result': dict(order_result)}
            raise AssertionError(f'unexpected request: {method} {path}')
        positions = iter([0.0, 1.0])
        with patch.object(bot, 'req', side_effect=fake_req), \
             patch.object(bot, 'ensure_no_open_orders', return_value=True), \
             patch.object(bot, 'read_stable_position', side_effect=lambda: next(positions)), \
             patch.object(bot.time, 'sleep', return_value=None):
            ok = bot.execute_target_position(journal, 1, False, 'MOCK ENTRY')
        check('order intent persisted before mocked POST', persisted_before_post == [True])
        check('successful mock order uses one POST and correct product/lot', ok and len(post_payloads) == 1 and post_payloads[0]['product_id'] == 131253 and post_payloads[0]['size'] == 1)
        check('filled order reconciles journal to exchange position', journal['managed_position'] == 1 and journal['pending_order'] is None)

        # 2. Ambiguous POST timeout: same client ID lookup only; never replay POST.
        journal2 = bot.default_execution_journal()
        post_count = {'n': 0}
        def ambiguous_req(method, path, payload=None):
            if method == 'POST':
                post_count['n'] += 1
                raise bot.APIError('simulated ambiguous POST timeout', transient=True)
            raise AssertionError(f'unexpected raw request: {method} {path}')
        with patch.object(bot, 'req', side_effect=ambiguous_req), \
             patch.object(bot, 'get_order_by_client_id', return_value=None), \
             patch.object(bot.time, 'sleep', return_value=None), \
             patch.object(bot, 'ORDER_CONFIRM_TIMEOUT', 0.02):
            result = bot.place_market_order(journal2, 'buy', 1, False, 0.0, 'MOCK AMBIGUOUS')
        saved2 = json.loads(pathlib.Path(bot.EXECUTION_JOURNAL_FILE).read_text())
        check('ambiguous POST attempted exactly once', post_count['n'] == 1)
        check('ambiguous POST is not resent and unresolved intent remains durable', result is None and saved2['pending_order']['phase'] == 'post_outcome_unknown')

        # 3. Restart recovery of a known client order ID and confirmed exchange fill.
        journal3 = bot.default_execution_journal()
        journal3['pending_order'] = {
            'client_order_id': 'xautbot-recovery1234567890', 'product_id': 131253,
            'side': 'buy', 'size': 1, 'reduce_only': False, 'before_position': 0.0,
            'purpose': 'MOCK_RECOVERY', 'phase': 'post_outcome_unknown', 'exchange_order_id': None
        }
        bot.save_execution_journal(journal3)
        recovered_order = {
            'id': 9001, 'product_id': 131253,
            'client_order_id': journal3['pending_order']['client_order_id'],
            'side': 'buy', 'size': 1, 'unfilled_size': 0,
            'order_type': 'market_order', 'reduce_only': False,
            'state': 'closed'
        }
        with patch.object(bot, 'get_order_by_client_id', return_value=recovered_order), \
             patch.object(bot, 'read_stable_position', return_value=1.0):
            recovered = bot.recover_pending_order(journal3)
        check('restart recovers original order by client ID without resubmission', recovered and journal3['pending_order'] is None and journal3['managed_position'] == 1)

        # 4. Exchange/local position mismatch must stop before strategy orders.
        mismatch_journal = bot.default_execution_journal()
        with patch.object(bot, 'recover_pending_order', return_value=True), \
             patch.object(bot, 'ensure_no_open_orders', return_value=True), \
             patch.object(bot, 'read_stable_position', return_value=1.0):
            blocked = False
            try:
                bot.startup_live_reconciliation(mismatch_journal, True)
            except bot.RecoveryRequired:
                blocked = True
        check('startup blocks exchange/local position mismatch', blocked)
    finally:
        os.chdir(old_cwd)

print(f'LIVE MOCK EXECUTION SAFETY SUITE: ALL {len(passed)} CHECKS PASSED (offline; no Delta API calls; no real orders).')

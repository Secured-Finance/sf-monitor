#!/usr/bin/env python3
"""Tests for the Filecoin monitor bridge.

Run with: npm run test:adapter   (or: python3 -m unittest discover -s adapters)

Stdlib unittest on purpose - the monitor image has no test tooling and this must stay
runnable anywhere Python 3 is, with no install step. `bridge()` takes its upstream
`send` as an argument, so every case here is driven by a fake upstream and makes no
network calls.
"""
import json
import unittest

from filecoin_adapter import ZERO, BloomCache, bridge, empty_epoch

BLOOM_WITH_LOGS = '0x' + '00' * 255 + '01'
BLOOM_EMPTY = '0x' + '00' * 256

NOW = 1_700_000_000.0
FINALIZED_HEIGHT = 6_446_986
# Mainnet's real finality lag is ~12 min; keep the template comfortably inside the
# default 30-minute staleness cutoff.
FINALIZED_TIMESTAMP = int(NOW - 700)
MAX_AGE = 1800

FINALIZED_BLOCK = {
    'number': hex(FINALIZED_HEIGHT),
    'timestamp': hex(FINALIZED_TIMESTAMP),
    'hash': '0x' + 'ab' * 32,
    'parentHash': '0x' + 'cd' * 32,
    'sha3Uncles': '0x' + 'ef' * 32,
    'stateRoot': '0x' + '11' * 32,
    'transactionsRoot': '0x' + '22' * 32,
    'receiptsRoot': '0x' + '33' * 32,
    'mixHash': '0x' + '44' * 32,
    'logsBloom': '0x' + '55' * 256,
    'gasUsed': '0x5208',
    'gasLimit': '0x1c9c380',
    'miner': '0x' + '66' * 20,
    'extraData': '0x',
    'transactions': ['0x' + '77' * 32],
    'uncles': [],
}


def override_key(method, params):
    """Hashable key for the override table.

    eth_call and eth_getLogs take an object as their first param, so it has to be
    normalised rather than used as a dict key directly.
    """
    if not params:
        return (method, None)
    first = params[0]
    return (method, first if isinstance(first, str) else json.dumps(first, sort_keys=True))


def null_round_error(height, data=True):
    error = {'code': 12, 'message': f'requested epoch was a null round ({height})'}
    if data:
        error['data'] = height
    return error


class FakeUpstream:
    """Minimal Lotus-shaped upstream. Records calls; echoes the request id."""

    def __init__(self, finalized=None, null_rounds=(), overrides=None):
        self.finalized = FINALIZED_BLOCK if finalized is None else finalized
        self.null_rounds = set(null_rounds)
        self.overrides = overrides or {}
        self.calls = []

    def __call__(self, query):
        self.calls.append(query)
        method = query['method']
        params = query.get('params') or []
        if override_key(method, params) in self.overrides:
            return {
                'jsonrpc': '2.0',
                'id': query.get('id'),
                **self.overrides[override_key(method, params)],
            }

        if method == 'eth_chainId':
            return {'jsonrpc': '2.0', 'id': query.get('id'), 'result': '0x13a'}
        if method == 'eth_call':
            return {'jsonrpc': '2.0', 'id': query.get('id'), 'result': '0x' + '00' * 31 + '04'}
        if method == 'eth_getLogs':
            return {'jsonrpc': '2.0', 'id': query.get('id'), 'result': []}
        if method == 'eth_getBlockByNumber':
            tag = params[0]
            if tag == 'finalized':
                return {'jsonrpc': '2.0', 'id': query.get('id'), 'result': self.finalized}
            height = int(tag, 16)
            if height in self.null_rounds:
                return {'jsonrpc': '2.0', 'id': query.get('id'), 'error': null_round_error(height)}
            block = dict(self.finalized, number=hex(height))
            return {'jsonrpc': '2.0', 'id': query.get('id'), 'result': block}
        raise AssertionError(f'unexpected upstream call: {method}')


def call(request, upstream, max_age=MAX_AGE, now=NOW):
    return bridge(request, upstream, max_age, now=now)


def req(method, params=None, id=1):
    return {'jsonrpc': '2.0', 'id': id, 'method': method, 'params': params or []}


class TestAllowlist(unittest.TestCase):
    def test_disallowed_method_is_refused_without_contacting_upstream(self):
        upstream = FakeUpstream()
        answer = call(req('eth_sendRawTransaction', ['0xdeadbeef']), upstream)
        self.assertEqual(answer['error']['code'], -32601)
        self.assertEqual(upstream.calls, [])

    def test_eth_call_passes_through_untranslated(self):
        # Lotus serves eth_call at a null-round height fine (state resolves to the
        # nearest tipset), so the trigger scripts need no special handling.
        upstream = FakeUpstream(null_rounds=[FINALIZED_HEIGHT - 5])
        answer = call(req('eth_call', [{'to': '0x' + '11' * 20, 'data': '0x'}, hex(FINALIZED_HEIGHT - 5)]), upstream)
        self.assertNotIn('error', answer)
        self.assertTrue(answer['result'].startswith('0x'))


class TestWatermark(unittest.TestCase):
    def test_block_number_reports_finalized_plus_one(self):
        # confirmation_blocks=1 cancels the +1 back to exactly `finalized`.
        answer = call(req('eth_blockNumber'), FakeUpstream())
        self.assertEqual(int(answer['result'], 16), FINALIZED_HEIGHT + 1)

    def test_stale_finalized_watermark_is_rejected(self):
        stale = dict(FINALIZED_BLOCK, timestamp=hex(int(NOW - MAX_AGE - 60)))
        answer = call(req('eth_blockNumber'), FakeUpstream(finalized=stale))
        self.assertEqual(answer['error']['code'], -32098)

    def test_same_watermark_is_accepted_under_a_larger_cutoff(self):
        # Calibration's finality lag is ~98 min, so it needs a cutoff well above
        # mainnet's - this is why the cutoff is configurable rather than hardcoded.
        age = MAX_AGE + 3600
        lagging = dict(FINALIZED_BLOCK, timestamp=hex(int(NOW - age)))
        self.assertEqual(call(req('eth_blockNumber'), FakeUpstream(finalized=lagging))['error']['code'], -32098)
        answer = call(req('eth_blockNumber'), FakeUpstream(finalized=lagging), max_age=age + 600)
        self.assertEqual(int(answer['result'], 16), FINALIZED_HEIGHT + 1)

    def test_future_finalized_watermark_is_rejected(self):
        future = dict(FINALIZED_BLOCK, timestamp=hex(int(NOW + 600)))
        answer = call(req('eth_blockNumber'), FakeUpstream(finalized=future))
        self.assertEqual(answer['error']['code'], -32098)


class TestNullRounds(unittest.TestCase):
    def test_finalized_null_round_becomes_an_empty_block(self):
        height = FINALIZED_HEIGHT - 10
        upstream = FakeUpstream(null_rounds=[height])
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        block = answer['result']
        self.assertEqual(int(block['number'], 16), height)
        self.assertEqual(block['transactions'], [])
        self.assertEqual(block['gasUsed'], '0x0')
        self.assertEqual(block['hash'], ZERO)
        self.assertEqual(block['parentHash'], ZERO)
        self.assertEqual(block['logsBloom'], '0x' + '00' * 256)
        # 10 epochs before the template, at 30s per epoch.
        self.assertEqual(int(block['timestamp'], 16), FINALIZED_TIMESTAMP - 300)

    def test_unfinalized_null_round_is_not_synthesized(self):
        # Beyond the finalized watermark we must never turn absent data into success.
        height = FINALIZED_HEIGHT + 5
        upstream = FakeUpstream(null_rounds=[height])
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(answer['error']['code'], 12)
        self.assertNotIn('result', answer)

    def test_null_round_error_for_a_different_epoch_is_rejected(self):
        height = FINALIZED_HEIGHT - 10
        upstream = FakeUpstream(
            overrides={('eth_getBlockByNumber', hex(height)): {'error': null_round_error(height + 1)}}
        )
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(answer['error']['code'], -32098)

    def test_matched_on_code_not_message_wording(self):
        # The code is the stable contract; upstreams may reword the prose.
        height = FINALIZED_HEIGHT - 10
        upstream = FakeUpstream(
            overrides={
                ('eth_getBlockByNumber', hex(height)): {
                    'error': {'code': 12, 'message': 'null round, reworded upstream', 'data': height}
                }
            }
        )
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(int(answer['result']['number'], 16), height)

    def test_other_upstream_errors_are_not_synthesized(self):
        height = FINALIZED_HEIGHT - 10
        upstream = FakeUpstream(
            overrides={
                ('eth_getBlockByNumber', hex(height)): {
                    'error': {'code': -32000, 'message': 'internal error'}
                }
            }
        )
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(answer['error']['code'], -32000)
        self.assertNotIn('result', answer)

    def test_null_round_without_data_field_still_synthesizes(self):
        height = FINALIZED_HEIGHT - 10
        upstream = FakeUpstream(
            overrides={('eth_getBlockByNumber', hex(height)): {'error': null_round_error(height, data=False)}}
        )
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(int(answer['result']['number'], 16), height)


class TestUpstreamValidation(unittest.TestCase):
    def test_envelope_id_mismatch_is_rejected(self):
        upstream = FakeUpstream(
            overrides={('eth_getBlockByNumber', hex(FINALIZED_HEIGHT)): {'result': FINALIZED_BLOCK, 'id': 999}}
        )
        answer = call(req('eth_getBlockByNumber', [hex(FINALIZED_HEIGHT), True]), upstream)
        self.assertEqual(answer['error']['code'], -32098)

    def test_wrong_block_height_is_rejected(self):
        height = FINALIZED_HEIGHT - 3
        upstream = FakeUpstream(
            overrides={
                ('eth_getBlockByNumber', hex(height)): {
                    'result': dict(FINALIZED_BLOCK, number=hex(height + 1))
                }
            }
        )
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(answer['error']['code'], -32098)

    def test_block_without_identity_is_rejected(self):
        height = FINALIZED_HEIGHT - 3
        upstream = FakeUpstream(
            overrides={
                ('eth_getBlockByNumber', hex(height)): {
                    'result': {k: v for k, v in FINALIZED_BLOCK.items() if k != 'hash'}
                }
            }
        )
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(answer['error']['code'], -32098)

    def test_ambiguous_error_and_result_is_rejected(self):
        height = FINALIZED_HEIGHT - 3
        upstream = FakeUpstream(
            overrides={
                ('eth_getBlockByNumber', hex(height)): {
                    'result': FINALIZED_BLOCK,
                    'error': {'code': 12, 'message': 'both'},
                }
            }
        )
        answer = call(req('eth_getBlockByNumber', [hex(height), True]), upstream)
        self.assertEqual(answer['error']['code'], -32098)

    def test_log_outside_requested_range_is_rejected(self):
        low, high = FINALIZED_HEIGHT - 5, FINALIZED_HEIGHT - 5
        stray = {
            'blockNumber': hex(high + 4),
            'blockHash': '0x' + '88' * 32,
            'transactionHash': '0x' + '99' * 32,
        }
        upstream = FakeUpstream(
            overrides={
                override_key('eth_getLogs', [{'fromBlock': hex(low), 'toBlock': hex(high)}]): {
                    'result': [stray]
                }
            }
        )
        answer = call(req('eth_getLogs', [{'fromBlock': hex(low), 'toBlock': hex(high)}]), upstream)
        self.assertEqual(answer['error']['code'], -32098)

    def test_logs_inside_range_pass(self):
        low = high = FINALIZED_HEIGHT - 5
        entry = {
            'blockNumber': hex(low),
            'blockHash': '0x' + '88' * 32,
            'transactionHash': '0x' + '99' * 32,
        }

        def send(query):
            return {'jsonrpc': '2.0', 'id': query['id'], 'result': [entry]}

        answer = call(req('eth_getLogs', [{'fromBlock': hex(low), 'toBlock': hex(high)}]), send)
        self.assertEqual(answer['result'], [entry])


class TestSilentlyEmptyLogs(unittest.TestCase):
    """The failure mode that loses alerts silently: a node without history for a
    height answers eth_getLogs with [] instead of an error. Measured on the public
    GLIF pool at ~72h depth in 4 of 8 attempts."""

    HEIGHT = FINALIZED_HEIGHT - 20

    def _send(self, bloom, logs):
        def send(query):
            if query['method'] == 'eth_getLogs':
                return {'jsonrpc': '2.0', 'id': query['id'], 'result': logs}
            block = dict(FINALIZED_BLOCK, number=hex(self.HEIGHT), logsBloom=bloom)
            return {'jsonrpc': '2.0', 'id': query['id'], 'result': block}

        return send

    def _get_logs(self, extra=None):
        f = {'fromBlock': hex(self.HEIGHT), 'toBlock': hex(self.HEIGHT)}
        f.update(extra or {})
        return req('eth_getLogs', [f])

    def test_empty_logs_contradicting_a_non_empty_bloom_is_rejected(self):
        answer = call(self._get_logs(), self._send(BLOOM_WITH_LOGS, []))
        self.assertEqual(answer['error']['code'], -32098)

    def test_empty_logs_matching_an_empty_bloom_passes(self):
        answer = call(self._get_logs(), self._send(BLOOM_EMPTY, []))
        self.assertEqual(answer['result'], [])

    def test_non_empty_logs_are_never_second_guessed(self):
        entry = {
            'blockNumber': hex(self.HEIGHT),
            'blockHash': '0x' + '88' * 32,
            'transactionHash': '0x' + '99' * 32,
        }
        answer = call(self._get_logs(), self._send(BLOOM_WITH_LOGS, [entry]))
        self.assertEqual(answer['result'], [entry])

    def test_address_filtered_empty_result_is_legitimate(self):
        # OZ Monitor queries unfiltered, but the trigger scripts may not - a filtered
        # query can correctly match nothing even when the block has other logs.
        answer = call(
            self._get_logs({'address': ['0x' + '11' * 20]}), self._send(BLOOM_WITH_LOGS, [])
        )
        self.assertEqual(answer['result'], [])

    def test_multi_block_range_is_not_attributable(self):
        f = {'fromBlock': hex(self.HEIGHT), 'toBlock': hex(self.HEIGHT + 5)}
        answer = call(req('eth_getLogs', [f]), self._send(BLOOM_WITH_LOGS, []))
        self.assertEqual(answer['result'], [])

    def test_bloom_is_reused_from_the_preceding_block_fetch(self):
        # The watcher fetches the block, then its logs. The second call must not need
        # another upstream round trip to know whether the block had logs.
        blooms = BloomCache()
        upstream = FakeUpstream(
            overrides={
                override_key('eth_getBlockByNumber', [hex(self.HEIGHT)]): {
                    'result': dict(FINALIZED_BLOCK, number=hex(self.HEIGHT), logsBloom=BLOOM_WITH_LOGS)
                }
            }
        )
        bridge(req('eth_getBlockByNumber', [hex(self.HEIGHT), True]), upstream, MAX_AGE, now=NOW, blooms=blooms)
        self.assertIs(blooms.get(self.HEIGHT), True)

        calls_before = len(upstream.calls)

        def send(query):
            if query['method'] == 'eth_getLogs':
                return {'jsonrpc': '2.0', 'id': query['id'], 'result': []}
            return upstream(query)

        answer = bridge(self._get_logs(), send, MAX_AGE, now=NOW, blooms=blooms)
        self.assertEqual(answer['error']['code'], -32098)
        self.assertEqual(len(upstream.calls), calls_before, 'should not re-fetch the header')

    def test_synthesized_null_epoch_has_an_empty_bloom_so_empty_logs_pass(self):
        blooms = BloomCache()
        height = FINALIZED_HEIGHT - 10
        upstream = FakeUpstream(null_rounds=[height])
        synthesized = bridge(
            req('eth_getBlockByNumber', [hex(height), True]), upstream, MAX_AGE, now=NOW, blooms=blooms
        )
        self.assertEqual(synthesized['result']['logsBloom'], BLOOM_EMPTY)

        def send(query):
            if query['method'] == 'eth_getLogs':
                return {'jsonrpc': '2.0', 'id': query['id'], 'result': []}
            return upstream(query)

        f = {'fromBlock': hex(height), 'toBlock': hex(height)}
        answer = bridge(req('eth_getLogs', [f]), send, MAX_AGE, now=NOW, blooms=blooms)
        self.assertEqual(answer['result'], [])


class TestEmptyLogEvidenceFailsClosed(unittest.TestCase):
    """When the bloom isn't cached, the fallback header decides whether an empty log
    result is trusted - so unavailable or unverifiable evidence must reject, never pass.
    """

    HEIGHT = FINALIZED_HEIGHT - 30

    def _call(self, header_answer):
        """header_answer: dict to return for the fallback header, or an exception."""

        def send(query):
            if query['method'] == 'eth_getLogs':
                return {'jsonrpc': '2.0', 'id': query['id'], 'result': []}
            if isinstance(header_answer, Exception):
                raise header_answer
            return dict(header_answer, id=header_answer.get('id', query['id']))

        f = {'fromBlock': hex(self.HEIGHT), 'toBlock': hex(self.HEIGHT)}
        return call(req('eth_getLogs', [f]), send)

    def _header(self, **over):
        block = dict(FINALIZED_BLOCK, number=hex(self.HEIGHT), logsBloom=BLOOM_EMPTY)
        block.update(over.pop('block', {}))
        return {'jsonrpc': '2.0', 'result': block, **over}

    def test_header_request_timing_out_is_rejected(self):
        answer = self._call(TimeoutError('upstream timed out'))
        self.assertEqual(answer['error']['code'], -32098)

    def test_header_for_the_wrong_height_is_rejected(self):
        answer = self._call(self._header(block={'number': hex(self.HEIGHT + 1)}))
        self.assertEqual(answer['error']['code'], -32098)

    def test_header_error_envelope_is_rejected(self):
        answer = self._call({'jsonrpc': '2.0', 'error': {'code': -32000, 'message': 'nope'}})
        self.assertEqual(answer['error']['code'], -32098)

    def test_header_with_mismatched_envelope_id_is_rejected(self):
        answer = self._call(self._header(id=4242))
        self.assertEqual(answer['error']['code'], -32098)

    def test_header_without_block_identity_is_rejected(self):
        block = {k: v for k, v in FINALIZED_BLOCK.items() if k != 'hash'}
        block.update(number=hex(self.HEIGHT), logsBloom=BLOOM_EMPTY)
        answer = self._call({'jsonrpc': '2.0', 'result': block})
        self.assertEqual(answer['error']['code'], -32098)

    def test_valid_header_with_empty_bloom_is_accepted(self):
        answer = self._call(self._header())
        self.assertEqual(answer['result'], [])

    def test_valid_header_reporting_logs_is_rejected(self):
        answer = self._call(self._header(block={'logsBloom': BLOOM_WITH_LOGS}))
        self.assertEqual(answer['error']['code'], -32098)


class TestNullRoundDoesNotStallOnTheLogsCheck(unittest.TestCase):
    """Regression guard for the interaction between the two fixes: tightening the
    empty-log check would turn every null round into a stall unless the synthesized
    epoch is cached as genuinely empty."""

    def test_null_round_then_empty_logs_passes_via_the_cache(self):
        blooms = BloomCache()
        height = FINALIZED_HEIGHT - 10
        upstream = FakeUpstream(null_rounds=[height])
        synthesized = bridge(
            req('eth_getBlockByNumber', [hex(height), True]), upstream, MAX_AGE, now=NOW, blooms=blooms
        )
        self.assertEqual(int(synthesized['result']['number'], 16), height)
        self.assertIs(blooms.get(height), False, 'null epoch must be cached as empty')

        # Upstream still has no header for a null round, so if the cache were not
        # consulted this would now (correctly) reject - and null rounds would stall.
        def send(query):
            if query['method'] == 'eth_getLogs':
                return {'jsonrpc': '2.0', 'id': query['id'], 'result': []}
            return upstream(query)

        f = {'fromBlock': hex(height), 'toBlock': hex(height)}
        answer = bridge(req('eth_getLogs', [f]), send, MAX_AGE, now=NOW, blooms=blooms)
        self.assertEqual(answer['result'], [])


class TestChainConfirmation(unittest.TestCase):
    def setUp(self):
        import filecoin_adapter

        self.mod = filecoin_adapter
        self._backoff = filecoin_adapter.STARTUP_BACKOFF_SECONDS
        filecoin_adapter.STARTUP_BACKOFF_SECONDS = 0

    def tearDown(self):
        self.mod.STARTUP_BACKOFF_SECONDS = self._backoff

    def test_transient_error_envelope_is_retried_then_succeeds(self):
        calls = []

        def send(query):
            calls.append(query)
            if len(calls) < 3:
                return {'jsonrpc': '2.0', 'id': query['id'], 'error': {'code': -32000, 'message': 'busy'}}
            return {'jsonrpc': '2.0', 'id': query['id'], 'result': '0x13a'}

        self.mod.confirm_chain(send, 314)  # must not raise
        self.assertEqual(len(calls), 3)

    def test_wrong_chain_exits_immediately_without_retrying(self):
        calls = []

        def send(query):
            calls.append(query)
            return {'jsonrpc': '2.0', 'id': query['id'], 'result': '0x4cb2f'}  # calibration

        with self.assertRaises(SystemExit):
            self.mod.confirm_chain(send, 314)
        self.assertEqual(len(calls), 1, 'a wrong chain is config error, not a blip')

    def test_unreachable_upstream_exits_after_bounded_attempts(self):
        calls = []

        def send(query):
            calls.append(query)
            raise ConnectionError('refused')

        with self.assertRaises(SystemExit):
            self.mod.confirm_chain(send, 314)
        self.assertEqual(len(calls), self.mod.STARTUP_ATTEMPTS)


class TestEmptyEpochShape(unittest.TestCase):
    def test_every_zeroed_identity_field_is_32_bytes(self):
        block = empty_epoch(FINALIZED_HEIGHT - 1, FINALIZED_BLOCK)
        for key in ['hash', 'parentHash', 'sha3Uncles', 'stateRoot', 'transactionsRoot', 'receiptsRoot', 'mixHash']:
            self.assertEqual(len(block[key]), 66, key)
        self.assertEqual(len(block['logsBloom']), 2 + 512)

    def test_template_is_not_mutated(self):
        before = dict(FINALIZED_BLOCK)
        empty_epoch(FINALIZED_HEIGHT - 1, FINALIZED_BLOCK)
        self.assertEqual(FINALIZED_BLOCK, before)

    def test_marker_in_extra_data(self):
        block = empty_epoch(FINALIZED_HEIGHT - 1, FINALIZED_BLOCK)
        self.assertIn(b'sf-monitor-null-epoch'.hex(), block['extraData'])


if __name__ == '__main__':
    unittest.main()

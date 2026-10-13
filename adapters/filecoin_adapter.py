#!/usr/bin/env python3
"""MONITOR-ONLY Filecoin JSON-RPC bridge for OZ Monitor. Never use with wallets.

Why this exists
---------------
OZ Monitor's EVM client is a *dense* block watcher: it requests every height in a
range via eth_getBlockByNumber and collects them with `try_collect()`, so a single
failing height fails the whole batch. The watcher then returns Err *without*
advancing the stored cursor, so the next tick retries the identical range forever.

Filecoin has null rounds - epochs where no block was produced. Lotus answers
eth_getBlockByNumber for such an epoch with a hard JSON-RPC error (code 12), not an
empty block. Measured against mainnet, null rounds are ~1.25% of epochs (~36/day,
and bursty), so an unadapted monitor stalls permanently within roughly 40 minutes of
starting. Verified identical on GLIF, dRPC and Ankr - this is Lotus behaviour, not a
provider quirk, and OZ Monitor's gap-tolerant `Sparse` stream kind is not available
to the EVM client.

This bridge sits on loopback between the monitor and the real RPC and does two
things:

1. Translates a confirmed null round into an *empty* block, so the unmodified dense
   watcher advances across it. An empty block matches no monitor condition, so this
   can never invent an alert - only let the cursor move.
2. Reports eth_blockNumber as (finalized epoch + 1). Paired with
   confirmation_blocks=1 in config/networks/filecoin_*.json, the watcher's
   `latest - confirmation_blocks` arithmetic yields an inclusive upper bound of
   exactly the finalized epoch, so the monitor only ever reads final history.

Everything else fails closed: a read-only method allowlist, upstream envelope and
identity validation, and a staleness guard on the finalized watermark. No
transactions, signing, credentials, subscriptions or arbitrary RPC forwarding.

Operational notes
-----------------
- The finalized watermark sets alert latency, and it differs sharply per network:
  ~23 epochs (~12 min) on mainnet, but ~196 epochs (~98 min) on Calibration. The
  staleness cutoff is therefore configurable and must sit comfortably above the
  network's real finality lag, or the guard rejects every watermark read and halts
  monitoring. It is an operational choice, not a Filecoin SLA.
- Failing closed means a stalled monitor rather than a wrong alert, which is the
  right trade for a safety monitor - but it is silent. Rejections are logged to
  stderr with their cause, /live backs a liveness probe, and /ready reports upstream
  reachability, so a stall is diagnosable rather than invisible.
"""
import argparse
import copy
import json
import os
import sys
import threading
import time
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Read-only subset the monitor and the Python trigger scripts actually need.
# eth_call is included for config/triggers/scripts/total_deposits_check.py, which
# resolves token addresses and balances live; Lotus serves eth_call at a null-round
# height fine (it resolves state to the nearest tipset), so it needs no translation.
ALLOWED = {
    'net_version',
    'eth_chainId',
    'eth_getBlockByNumber',
    'eth_getLogs',
    'eth_getTransactionReceipt',
    'eth_call',
    'eth_blockNumber',
}

ZERO = '0x' + '00' * 32
EPOCH_SECONDS = 30
NULL_ROUND_ERROR_CODE = 12

# Bounded startup retry: a transient upstream blip should not CrashLoopBackOff the
# sidecar, but a genuinely wrong chain or URL must still surface instead of serving
# a silently wrong chain's data forever.
STARTUP_ATTEMPTS = 10
STARTUP_BACKOFF_SECONDS = 6

# /ready probes upstream, so cache it rather than issuing an RPC per probe.
READY_CACHE_SECONDS = 15


class Rejected(Exception):
    """Upstream data failed validation - respond with an error, never a guess."""


def log(**fields):
    print(json.dumps(fields), flush=True)


def log_error(**fields):
    print(json.dumps(fields), file=sys.stderr, flush=True)


def empty_epoch(number, template):
    """An empty block standing in for a null round at `number`.

    Shaped from a real finalized block so every field the monitor's EVMBlock
    deserializer expects is present and correctly sized, with anything
    block-identifying zeroed and all content removed. Only 32-byte fields are
    zeroed with ZERO; logsBloom is 256 bytes.
    """
    block = copy.deepcopy(template)
    for key in [
        'hash',
        'parentHash',
        'sha3Uncles',
        'stateRoot',
        'transactionsRoot',
        'receiptsRoot',
        'mixHash',
    ]:
        block[key] = ZERO
    block.update(
        number=hex(number),
        transactions=[],
        uncles=[],
        gasUsed='0x0',
        logsBloom='0x' + '00' * 256,
        extraData='0x' + b'sf-monitor-null-epoch'.hex(),
    )
    # Extrapolate from the template's own height/timestamp; `number` is always at or
    # below it, so this walks backwards at Filecoin's fixed epoch duration.
    block['timestamp'] = hex(
        int(template['timestamp'], 16)
        + (number - int(template['number'], 16)) * EPOCH_SECONDS
    )
    return block


def bridge(request, send, max_finalized_age, now=None, blooms=None):
    """Handle one request, validating every upstream answer. Fails closed."""
    blooms = BloomCache() if blooms is None else blooms
    try:
        return _bridge(request, _validating(send, max_finalized_age, now, blooms), blooms)
    except Rejected as rejection:
        # Logged, because the alternative is a silent permanent stall: the watcher
        # retries the same range forever and nothing says why.
        log_error(
            rejected=str(rejection),
            method=request.get('method'),
            params=request.get('params'),
        )
        return {
            'jsonrpc': '2.0',
            'id': request.get('id'),
            'error': {
                'code': -32098,
                'message': 'Missing, stale, mismatched or unavailable upstream data; response rejected',
            },
        }
    except Exception as error:  # noqa: BLE001 - must not leak internals to the caller
        log_error(
            adapter_failure=f'{type(error).__name__}: {error}',
            method=request.get('method'),
        )
        return {
            'jsonrpc': '2.0',
            'id': request.get('id'),
            'error': {'code': -32098, 'message': 'Filecoin upstream/adapter failure'},
        }


def _validating(send, max_finalized_age, now=None, blooms=None):
    """Wraps `send` so every upstream answer is checked before it is trusted."""
    blooms = BloomCache() if blooms is None else blooms

    def checked(query):
        answer = send(query)
        if (
            not isinstance(answer, dict)
            or answer.get('jsonrpc') != '2.0'
            or answer.get('id') != query.get('id')
        ):
            raise Rejected('Mismatched upstream JSON-RPC envelope')
        if 'error' in answer:
            if 'result' in answer:
                raise Rejected('Ambiguous error/result envelope')
            # Errors are returned for the caller to interpret (null rounds are an
            # expected one); they are never treated as success.
            return answer
        if answer.get('result') is None:
            raise Rejected('Missing upstream result')

        method = query['method']
        params = query.get('params') or []
        result = answer['result']

        if method == 'eth_getBlockByNumber':
            _check_block(result, params, max_finalized_age, now)
            blooms.remember(result)
        elif method == 'eth_getTransactionReceipt':
            if (
                not isinstance(result, dict)
                or result.get('transactionHash', '').lower() != params[0].lower()
            ):
                raise Rejected('Wrong receipt identity')
        elif method == 'eth_getLogs':
            _check_logs(result, params)
            _reject_silently_empty_logs(result, params, blooms, send)
        return answer

    return checked


def _check_block(result, params, max_finalized_age, now):
    if not isinstance(result, dict) or not _is_hash(result.get('hash', '')):
        raise Rejected('Missing block identity')
    height = int(result['number'], 16)
    requested = params[0]
    if isinstance(requested, str) and requested.startswith('0x'):
        if height != int(requested, 16):
            raise Rejected('Wrong block height')
    if requested == 'finalized':
        age = (time.time() if now is None else now) - int(result['timestamp'], 16)
        if age < -60:
            raise Rejected(f'Finalized watermark is in the future by {-age:.0f}s')
        if age > max_finalized_age:
            raise Rejected(
                f'Finalized watermark is {age:.0f}s old (cutoff {max_finalized_age}s); '
                'monitoring halted rather than trusting stale history'
            )


class BloomCache:
    """Remembers, per height, whether a block header said it contains any logs.

    Populated from the eth_getBlockByNumber the watcher already makes for a height
    immediately before it asks for that height's logs, so the check below normally
    costs no extra upstream call.
    """

    def __init__(self, limit=512):
        self._has_logs = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def remember(self, block):
        bloom = block.get('logsBloom')
        number = block.get('number')
        if not isinstance(bloom, str) or not isinstance(number, str):
            return
        try:
            height, has_logs = int(number, 16), int(bloom, 16) != 0
        except ValueError:
            return
        with self._lock:
            self._has_logs[height] = has_logs
            self._has_logs.move_to_end(height)
            while len(self._has_logs) > self._limit:
                self._has_logs.popitem(last=False)

    def get(self, height):
        with self._lock:
            return self._has_logs.get(height)


def _reject_silently_empty_logs(result, params, blooms, send):
    """Catch an upstream that answers "no logs" for a block that has logs.

    A Filecoin node without history for a height returns an empty log array rather
    than an error (measured on the public GLIF pool: 4 of 8 attempts at ~72h depth).
    That is structurally valid, so no amount of envelope validation catches it - and
    it is the one failure mode that loses alerts *silently*: the monitor concludes the
    block held nothing, advances its cursor, and the event is gone for good.

    The block header's logsBloom is the cross-check. OZ Monitor's filter asks for a
    single block's logs with no address filter (`get_logs_for_blocks(n, n, None)`), so
    a non-zero bloom means the unfiltered answer must not be empty. Disagreement means
    the upstream is incomplete, and we fail closed - a stalled monitor is recoverable,
    a skipped block is not.
    """
    if result:
        return
    log_filter = params[0] if params else {}
    if not isinstance(log_filter, dict):
        return
    # A filtered query may legitimately match nothing, and a multi-block range can't be
    # attributed to one header, so only the unfiltered single-block case is checkable.
    if log_filter.get('address') or log_filter.get('topics'):
        return
    try:
        low = int(log_filter['fromBlock'], 16)
        high = int(log_filter['toBlock'], 16)
    except (KeyError, TypeError, ValueError):
        return
    if low != high:
        return

    has_logs = blooms.get(low)
    if has_logs is None:
        has_logs = _header_has_logs(send, low)
    if has_logs is None:
        # Unavailable evidence is not evidence of absence. Accepting [] here would
        # reopen exactly the silent skip this check exists to close, so the absence of
        # a trustworthy header is itself a rejection.
        raise Rejected(
            f'Cannot establish whether block {low} holds logs (header missing, '
            'unverifiable or at the wrong height); an empty eth_getLogs cannot be trusted'
        )
    if has_logs:
        raise Rejected(
            f'Block {low} header reports logs present but eth_getLogs returned none; '
            'upstream history is incomplete - refusing to advance past it'
        )


def _header_has_logs(send, height):
    """Whether the header at `height` reports any logs. None if undeterminable.

    This answer decides whether an empty log result is trusted, so the header backing
    it is validated as strictly as any other upstream response: envelope, block
    identity, and above all the height - a header for some *other* block says nothing
    about this one. None means "no trustworthy evidence", and the caller treats that as
    a rejection rather than a pass.
    """
    query = {'jsonrpc': '2.0', 'id': 0, 'method': 'eth_getBlockByNumber', 'params': [hex(height), False]}
    try:
        answer = send(query)
    except Exception:  # noqa: BLE001 - timeout/transport failure is "unknown"
        return None
    if (
        not isinstance(answer, dict)
        or answer.get('jsonrpc') != '2.0'
        or answer.get('id') != query['id']
        or 'error' in answer
    ):
        return None
    block = answer.get('result')
    if not isinstance(block, dict) or not _is_hash(block.get('hash', '')):
        return None
    try:
        if int(block.get('number'), 16) != height:
            return None
    except (TypeError, ValueError):
        return None
    bloom = block.get('logsBloom')
    if not isinstance(bloom, str):
        return None
    try:
        return int(bloom, 16) != 0
    except ValueError:
        return None


def _check_logs(result, params):
    if not isinstance(result, list):
        raise Rejected('Malformed logs')
    log_filter = params[0]
    low = int(log_filter['fromBlock'], 16)
    high = int(log_filter['toBlock'], 16)
    for entry in result:
        height = int(entry['blockNumber'], 16)
        if not low <= height <= high:
            raise Rejected('Log outside requested range')
        for key in ['blockHash', 'transactionHash']:
            if not _is_hash(entry.get(key, '')):
                raise Rejected('Missing log identity')


def _is_hash(value):
    if not isinstance(value, str) or not value.startswith('0x') or len(value) != 66:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _bridge(request, send, blooms):
    method = request.get('method')
    params = request.get('params') or []

    if method not in ALLOWED:
        return {
            'jsonrpc': '2.0',
            'id': request.get('id'),
            'error': {'code': -32601, 'message': 'Monitor read-only method allowlist'},
        }

    if method == 'eth_blockNumber':
        # The monitoring watermark: finalized + 1, cancelled back to exactly
        # `finalized` by confirmation_blocks=1 in the network config.
        answer = send({**request, 'method': 'eth_getBlockByNumber', 'params': ['finalized', False]})
        block = answer.get('result')
        if isinstance(block, dict):
            return {
                'jsonrpc': '2.0',
                'id': request.get('id'),
                'result': hex(int(block['number'], 16) + 1),
            }
        return answer

    answer = send(request)
    if method == 'eth_getBlockByNumber' and _is_height_request(params):
        height = int(params[0], 16)
        synthesized = _maybe_null_round(answer, height, request, send, blooms)
        if synthesized is not None:
            return synthesized
    return answer


def _is_height_request(params):
    return len(params) == 2 and isinstance(params[0], str) and params[0].startswith('0x')


def _maybe_null_round(answer, height, request, send, blooms):
    """An empty block for `height`, if upstream confirmed it is a finalized null round.

    Matched on the JSON-RPC error *code* rather than the human-readable message:
    the code is the stable contract, the message is prose that an upstream is free to
    reword. When `data` is present it must agree with the requested height, so an
    error about some other epoch can never be mistaken for this one.
    """
    error = answer.get('error') or {}
    if error.get('code') != NULL_ROUND_ERROR_CODE:
        return None
    if 'data' in error and error['data'] != height:
        raise Rejected(f'Null-round error for epoch {error["data"]}, requested {height}')

    # Require the epoch to be finalized before standing in for it. Missing or pruned
    # data must never be turned into success - only a settled null round.
    final = send({**request, 'method': 'eth_getBlockByNumber', 'params': ['finalized', False]})
    block = final.get('result')
    if not isinstance(block, dict):
        return None
    if height > int(block['number'], 16):
        return None

    log(null_epoch_placeholder=height, upstream_message=error.get('message'))
    synthesized = empty_epoch(height, block)
    # Record the null epoch as genuinely empty. Without this, the logs check below
    # would later find no cached bloom, re-ask upstream for a header that does not
    # exist (it is a null round), get nothing it can trust, and reject a correct
    # empty-log answer - turning every null round into a stall.
    blooms.remember(synthesized)
    return {'jsonrpc': '2.0', 'id': request.get('id'), 'result': synthesized}


def make_sender(upstream, timeout=10):
    def send(request):
        payload = json.dumps(request).encode()
        http_request = urllib.request.Request(
            upstream, data=payload, headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            return json.load(response)

    return send


def confirm_chain(send, expected_chain_id):
    """Assert upstream is the chain we were configured for, with bounded retry."""
    expected_hex = hex(expected_chain_id)
    last = None
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            answer = send({'jsonrpc': '2.0', 'id': 0, 'method': 'eth_chainId', 'params': []})
            actual = answer.get('result') if isinstance(answer, dict) else None
            if actual == expected_hex:
                log(upstream_chain_id=actual, expected=expected_hex, ok=True)
                return
            if isinstance(actual, str) and actual.startswith('0x'):
                # A valid response naming a different chain is a configuration error,
                # not a blip - fail immediately rather than retrying into it.
                raise SystemExit(
                    f'Upstream reports chain ID {actual}, expected {expected_hex} '
                    f'({expected_chain_id}); refusing to serve another chain\'s data'
                )
            # Anything else (an error envelope, a missing or malformed result) says
            # nothing about which chain this is - treat it as transient and retry,
            # instead of reading it as "chain ID None" and exiting on the first blip.
            raise Rejected(f'no usable chain id in upstream response: {answer!r}')
        except SystemExit:
            raise
        except Exception as error:  # noqa: BLE001 - transient upstream failure
            last = error
            log_error(
                startup_probe_failed=f'{type(error).__name__}: {error}',
                attempt=attempt,
                of=STARTUP_ATTEMPTS,
            )
            if attempt < STARTUP_ATTEMPTS:
                time.sleep(STARTUP_BACKOFF_SECONDS)
    raise SystemExit(f'Upstream unreachable after {STARTUP_ATTEMPTS} attempts: {last}')


def build_handler(send, expected_chain_id, max_finalized_age):
    expected_hex = hex(expected_chain_id)
    ready_state = {'at': 0.0, 'ok': False, 'detail': 'not probed yet'}
    ready_lock = threading.Lock()
    # Shared across requests: the watcher fetches a block and asks for its logs in two
    # separate calls, so the header's bloom has to outlive the first one.
    blooms = BloomCache()

    def ready():
        with ready_lock:
            if time.time() - ready_state['at'] < READY_CACHE_SECONDS:
                return ready_state['ok'], ready_state['detail']
            try:
                answer = send({'jsonrpc': '2.0', 'id': 0, 'method': 'eth_chainId', 'params': []})
                actual = answer.get('result')
                ok = actual == expected_hex
                detail = 'ok' if ok else f'chain id {actual} != {expected_hex}'
            except Exception as error:  # noqa: BLE001
                ok, detail = False, f'{type(error).__name__}: {error}'
            ready_state.update(at=time.time(), ok=ok, detail=detail)
            return ok, detail

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        # Keep-alive is implied by HTTP/1.1, so without a timeout a half-open
        # connection would pin a handler thread for good.
        timeout = 65

        def log_message(self, *args):
            pass  # access logs would drown the monitor's own output

        def _respond(self, status, body):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            # Liveness stays cheap and upstream-independent: it catches a wedged
            # server loop, which is what a restart actually fixes. Readiness, which
            # does probe upstream, is observability only - loopback traffic inside
            # the Pod is not gated by it.
            if self.path.startswith('/live'):
                self._respond(200, {'status': 'alive'})
            elif self.path.startswith('/ready'):
                ok, detail = ready()
                self._respond(200 if ok else 503, {'status': 'ready' if ok else 'unready', 'detail': detail})
            else:
                self._respond(404, {'error': 'not found'})

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError('Batch requests are not supported')
                body = bridge(request, send, max_finalized_age, blooms=blooms)
            except Exception as error:  # noqa: BLE001
                log_error(request_failure=f'{type(error).__name__}: {error}')
                body = {
                    'jsonrpc': '2.0',
                    'id': None,
                    'error': {'code': -32000, 'message': 'Filecoin upstream/adapter failure'},
                }
            self._respond(200, body)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=int(os.environ.get('FILECOIN_ADAPTER_PORT', 8547)))
    parser.add_argument(
        '--upstream',
        default=os.environ.get('FILECOIN_UPSTREAM_RPC_URL', 'https://api.node.glif.io/rpc/v1'),
    )
    parser.add_argument(
        '--chain-id',
        type=int,
        default=int(os.environ.get('FILECOIN_EXPECTED_CHAIN_ID', 314)),
        help='314 for Filecoin mainnet, 314159 for Calibration',
    )
    parser.add_argument(
        '--max-finalized-age',
        type=int,
        default=int(os.environ.get('FILECOIN_MAX_FINALIZED_AGE_SECONDS', 1800)),
        help='Reject the finalized watermark once it is older than this (seconds). '
        'Must exceed the network\'s real finality lag: ~12 min on mainnet, ~98 min on Calibration',
    )
    args = parser.parse_args(argv)

    send = make_sender(args.upstream)
    confirm_chain(send, args.chain_id)
    log(
        listening=f'127.0.0.1:{args.port}',
        chain_id=args.chain_id,
        max_finalized_age_seconds=args.max_finalized_age,
    )
    handler = build_handler(send, args.chain_id, args.max_finalized_age)
    ThreadingHTTPServer(('127.0.0.1', args.port), handler).serve_forever()


if __name__ == '__main__':
    main()

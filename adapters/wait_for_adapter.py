#!/usr/bin/env python3
"""Block until the Filecoin adapter is serving, so the monitor never starts half-blind.

Why this gate is required rather than nice to have: OZ Monitor builds its per-network
watchers once, at boot, in a loop that merely logs on failure (src/main.rs, v1.6.0):

    if let Ok(client) = client_pool.get_evm_client(&network).await {
        block_watcher.start_network_watcher(...)
    } else {
        error!("Failed to get EVM client for network: {}", network.slug);
    }

Nothing retries afterwards. So if the adapter has not yet bound its port when the
monitor initialises, Filecoin gets no watcher at all, nothing ever creates one, and the
process stays up looking perfectly healthy while that chain is simply unmonitored. A
readiness probe on the sidecar cannot prevent this: both containers start together, and
readiness gates Service endpoints, not a sibling container's startup.

Exiting non-zero is deliberate: a crash-looping monitor container is visible and
self-healing, whereas a monitor that started without its Filecoin watcher is neither.

Skipped (exit 0) when FILECOIN_ADAPTER_URL is unset, so a deployment with no Filecoin
network configured is unaffected.
"""
import os
import sys
import time
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 120
POLL_SECONDS = 1
PROBE_TIMEOUT_SECONDS = 2


def wait_for(url, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, sleep=time.sleep, opener=None):
    """True once `url` answers 2xx, False if it never does within the budget."""
    opener = opener or urllib.request.urlopen
    deadline_polls = max(1, int(timeout_seconds / POLL_SECONDS))
    for attempt in range(deadline_polls):
        try:
            opener(url, timeout=PROBE_TIMEOUT_SECONDS)
            return True
        except Exception:  # noqa: BLE001 - not up yet is the normal case here
            if attempt + 1 < deadline_polls:
                sleep(POLL_SECONDS)
    return False


def main(argv=None):
    base = os.environ.get('FILECOIN_ADAPTER_URL')
    if not base:
        print('FILECOIN_ADAPTER_URL unset; no Filecoin adapter to wait for', flush=True)
        return 0
    timeout_seconds = int(os.environ.get('FILECOIN_ADAPTER_WAIT_SECONDS', DEFAULT_TIMEOUT_SECONDS))
    url = base.rstrip('/') + '/live'
    print(f'waiting up to {timeout_seconds}s for the Filecoin adapter at {url}', flush=True)
    started = time.time()
    if wait_for(url, timeout_seconds):
        print(f'Filecoin adapter ready after {time.time() - started:.1f}s', flush=True)
        return 0
    sys.stderr.write(
        f'Filecoin adapter did not answer {url} within {timeout_seconds}s. Refusing to start the '
        'monitor, which would otherwise come up permanently missing its Filecoin watcher.\n'
    )
    return 1


if __name__ == '__main__':
    sys.exit(main())

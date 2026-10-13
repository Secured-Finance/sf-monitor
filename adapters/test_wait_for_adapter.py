#!/usr/bin/env python3
"""Tests for the startup gate, including the delayed-adapter case.

No sleeping and no sockets: `wait_for` takes its sleep function and its opener as
arguments, so a slow adapter is simulated by counting calls.
"""
import unittest

import wait_for_adapter
from wait_for_adapter import main, wait_for

URL = 'http://127.0.0.1:8547/live'


class Opener:
    """Fails for the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, url, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionRefusedError('not up yet')
        return object()


class TestWaitFor(unittest.TestCase):
    def test_ready_immediately(self):
        opener = Opener(0)
        slept = []
        self.assertTrue(wait_for(URL, 120, sleep=slept.append, opener=opener))
        self.assertEqual(opener.calls, 1)
        self.assertEqual(slept, [], 'should not sleep when already up')

    def test_adapter_that_starts_late_is_waited_out(self):
        # The case that actually bit us: both containers start together and the adapter
        # takes a while to bind because it verifies upstream first.
        opener = Opener(fail_times=30)
        slept = []
        self.assertTrue(wait_for(URL, 120, sleep=slept.append, opener=opener))
        self.assertEqual(opener.calls, 31)
        self.assertEqual(len(slept), 30)

    def test_adapter_that_never_comes_up_gives_up_within_budget(self):
        opener = Opener(fail_times=10_000)
        slept = []
        self.assertFalse(wait_for(URL, 5, sleep=slept.append, opener=opener))
        self.assertEqual(opener.calls, 5, 'one probe per second of budget')
        self.assertEqual(len(slept), 4, 'no trailing sleep after the last attempt')

    def test_budget_of_zero_still_probes_once(self):
        opener = Opener(0)
        self.assertTrue(wait_for(URL, 0, sleep=lambda _: None, opener=opener))
        self.assertEqual(opener.calls, 1)


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env = dict(wait_for_adapter.os.environ)
        self._wait = wait_for_adapter.wait_for

    def tearDown(self):
        wait_for_adapter.os.environ.clear()
        wait_for_adapter.os.environ.update(self._env)
        wait_for_adapter.wait_for = self._wait

    def _set(self, **env):
        for k in ('FILECOIN_ADAPTER_URL', 'FILECOIN_ADAPTER_WAIT_SECONDS'):
            wait_for_adapter.os.environ.pop(k, None)
        wait_for_adapter.os.environ.update(env)

    def test_skips_when_no_adapter_is_configured(self):
        self._set()
        wait_for_adapter.wait_for = lambda *a, **k: self.fail('must not probe')
        self.assertEqual(main(), 0)

    def test_exit_zero_when_adapter_becomes_ready(self):
        self._set(FILECOIN_ADAPTER_URL='http://127.0.0.1:8547')
        seen = {}

        def fake(url, timeout_seconds):
            seen['url'] = url
            return True

        wait_for_adapter.wait_for = fake
        self.assertEqual(main(), 0)
        self.assertEqual(seen['url'], URL, 'should probe /live, and tolerate a trailing slash')

    def test_trailing_slash_is_normalised(self):
        self._set(FILECOIN_ADAPTER_URL='http://127.0.0.1:8547/')
        seen = {}
        wait_for_adapter.wait_for = lambda url, timeout_seconds: seen.setdefault('url', url) or True
        main()
        self.assertEqual(seen['url'], URL)

    def test_exit_nonzero_when_adapter_never_readies(self):
        # Non-zero so the Pod crash-loops visibly instead of running without a
        # Filecoin watcher, which OZ Monitor would never create afterwards.
        self._set(FILECOIN_ADAPTER_URL='http://127.0.0.1:8547')
        wait_for_adapter.wait_for = lambda url, timeout_seconds: False
        self.assertEqual(main(), 1)

    def test_wait_budget_is_configurable(self):
        self._set(FILECOIN_ADAPTER_URL='http://127.0.0.1:8547', FILECOIN_ADAPTER_WAIT_SECONDS='7')
        seen = {}
        wait_for_adapter.wait_for = lambda url, timeout_seconds: seen.setdefault('t', timeout_seconds) or True
        main()
        self.assertEqual(seen['t'], 7)


if __name__ == '__main__':
    unittest.main()

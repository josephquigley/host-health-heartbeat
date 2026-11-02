import os, sys, socket, unittest, urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import os_health


class _Resp:
    def close(self):
        pass


def _http_error(code):
    return urllib.error.HTTPError("http://x/ping/t", code, "err", {}, None)


class PingRetry(unittest.TestCase):
    """A single lost ping must not cost a heartbeat: transient failures (timeouts,
    DNS, refused, 5xx/429) are retried with backoff; a definitive 4xx is not."""

    def setUp(self):
        self.sleeps = []
        p = mock.patch.object(os_health.time, "sleep", side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, effects):
        with mock.patch.object(os_health.urllib.request, "urlopen", side_effect=effects) as m:
            result = os_health.ping("http://x/ping/t", "", "body")
        return m, result

    def test_success_first_try_no_retry(self):
        m, ok = self._run([_Resp()])
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_timeout_then_success(self):
        m, ok = self._run([TimeoutError("timed out"), _Resp()])
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(self.sleeps, [os_health.PING_BACKOFF[0]])

    def test_dns_failure_is_retried(self):
        dns = urllib.error.URLError(socket.gaierror(-3, "Try again"))
        m, ok = self._run([dns, dns, _Resp()])
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 3)

    def test_connection_refused_is_retried(self):
        m, ok = self._run([urllib.error.URLError(ConnectionRefusedError(111, "refused")), _Resp()])
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 2)

    def test_5xx_and_429_are_retried(self):
        m, ok = self._run([_http_error(502), _http_error(429), _Resp()])
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 3)

    def test_4xx_is_not_retried(self):
        # 410 = monitor paused, 404 = bad token: retrying cannot help
        m, ok = self._run([_http_error(410)])
        self.assertFalse(ok)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_gives_up_after_all_attempts_and_never_raises(self):
        n = len(os_health.PING_BACKOFF) + 1
        m, ok = self._run([TimeoutError("timed out")] * n)
        self.assertFalse(ok)
        self.assertEqual(m.call_count, n)
        self.assertEqual(self.sleeps, list(os_health.PING_BACKOFF))

    def test_empty_url_is_noop(self):
        with mock.patch.object(os_health.urllib.request, "urlopen") as m:
            self.assertIsNone(os_health.ping("", "", "body"))
        m.assert_not_called()

    def test_worst_case_fits_well_inside_kernel_cadence(self):
        # every attempt times out: total must stay far below the 30-min KERNEL period
        worst = sum(os_health.PING_BACKOFF) + (len(os_health.PING_BACKOFF) + 1) * os_health.PING_TIMEOUT
        self.assertLess(worst, 300)


if __name__ == "__main__":
    unittest.main()

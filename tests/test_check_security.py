import os, sys, time, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "content"))
import os_health


def _updates(n):
    """Temp updates-available file with N standard security updates."""
    fd, p = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        f.write(f"{n} of these updates are standard security updates\n" if n
                else "0 updates can be applied immediately.\n")
    return p


def _stamp(age_seconds):
    """Temp stamp file whose mtime is `age_seconds` in the past."""
    fd, p = tempfile.mkstemp()
    os.close(fd)
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


class CheckSecurity(unittest.TestCase):
    def test_zero_pending_is_ok(self):
        ok, fails, info = os_health.check_security(_updates(0), _stamp(0), 172800)
        self.assertTrue(ok)
        self.assertEqual(fails, [])
        self.assertIn("0 pending", info[0])

    def test_pending_with_fresh_stamp_is_ok(self):
        ok, fails, info = os_health.check_security(_updates(5), _stamp(3600), 172800)
        self.assertTrue(ok)
        self.assertEqual(fails, [])
        self.assertIn("auto-update ran", info[0])

    def test_pending_with_stale_stamp_fails(self):
        ok, fails, info = os_health.check_security(_updates(5), _stamp(3 * 86400), 172800)
        self.assertFalse(ok)
        self.assertIn("pending", fails[0])
        self.assertIn("last ran", info[0])

    def test_pending_with_missing_stamp_fails(self):
        ok, fails, info = os_health.check_security(_updates(5), "/nonexistent/stamp", 172800)
        self.assertFalse(ok)
        self.assertIn("never ran / not installed", fails[0])

    def test_zero_pending_missing_stamp_is_ok(self):
        ok, fails, info = os_health.check_security(_updates(0), "/nonexistent/stamp", 172800)
        self.assertTrue(ok)
        self.assertEqual(fails, [])


if __name__ == "__main__":
    unittest.main()

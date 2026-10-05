import os, sys, time, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "content"))
import os_health


def _flag(age_seconds, pkgs=None):
    """Temp reboot-required file (plus optional .pkgs) whose mtime is `age_seconds` ago."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "reboot-required")
    with open(p, "w") as f:
        f.write("*** System restart required ***\n")
    if pkgs:
        with open(p + ".pkgs", "w") as f:
            f.write("\n".join(pkgs) + "\n")
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


class CheckReboot(unittest.TestCase):
    def test_no_flag_is_ok(self):
        ok, fails, info = os_health.check_reboot("/nonexistent/reboot-required", 0)
        self.assertTrue(ok)
        self.assertEqual(fails, [])
        self.assertIn("not required", info[0])

    def test_flag_without_grace_fails_immediately(self):
        ok, fails, info = os_health.check_reboot(_flag(60), 0)
        self.assertFalse(ok)
        self.assertIn("reboot required", fails[0])

    def test_flag_lists_packages(self):
        ok, fails, info = os_health.check_reboot(_flag(60, ["linux-image-generic", "libc6"]), 0)
        self.assertFalse(ok)
        self.assertIn("libc6", fails[0])
        self.assertIn("linux-image-generic", fails[0])

    def test_flag_inside_grace_is_ok(self):
        ok, fails, info = os_health.check_reboot(_flag(3 * 86400), 9 * 86400)
        self.assertTrue(ok)
        self.assertEqual(fails, [])
        self.assertIn("grace", info[0])

    def test_flag_past_grace_fails(self):
        ok, fails, info = os_health.check_reboot(_flag(10 * 86400), 9 * 86400)
        self.assertFalse(ok)
        self.assertIn("reboot required", fails[0])
        self.assertIn("9d", fails[0])

    def test_grace_boundary_still_ok(self):
        ok, _, _ = os_health.check_reboot(_flag(9 * 86400 - 60), 9 * 86400)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()

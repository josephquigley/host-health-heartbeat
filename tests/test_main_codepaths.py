import os, sys, json, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import os_health


class Selected(unittest.TestCase):
    def test_empty_is_all(self):
        self.assertEqual(os_health.selected([]), [n for n, _ in os_health.CHECKS])

    def test_kernel_only(self):
        self.assertEqual(os_health.selected(["KERNEL"]), ["KERNEL"])

    def test_case_insensitive_and_canonical_order(self):
        # given out of order + lowercase, returns CHECKS order
        self.assertEqual(os_health.selected(["reboot", "security"]), ["SECURITY", "REBOOT"])

    def test_unknown_is_fatal(self):
        with self.assertRaises(SystemExit):
            os_health.selected(["bogus"])


class OverallFailed(unittest.TestCase):
    def test_all_ok(self):
        self.assertFalse(os_health.overall_failed({"SECURITY": "ok", "REBOOT": "ok", "KERNEL": "ok"}))

    def test_one_fail(self):
        self.assertTrue(os_health.overall_failed({"SECURITY": "ok", "REBOOT": "fail", "KERNEL": "ok"}))

    def test_missing_category_treated_ok(self):
        self.assertFalse(os_health.overall_failed({"KERNEL": "ok"}))


class KernelCodepathMergesStatus(unittest.TestCase):
    """A kernel-only run must update KERNEL, preserve the persisted SECURITY/REBOOT
    status, and reflect the *merged* status in the Docker health-file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        logs = os.path.join(self.tmp, "log")
        os.mkdir(logs)
        for n in os_health.LOG_FILES:  # empty logs -> kernel clean (first sight seeds EOF)
            open(os.path.join(logs, n), "w").close()
        self._saved = {k: getattr(os_health, k) for k in
                       ("LOG_DIR", "STATE_FILE", "CATEGORY_STATUS_FILE", "HEALTH_FILE", "PING_URLS")}
        os_health.LOG_DIR = logs
        os_health.STATE_FILE = os.path.join(self.tmp, "offsets.json")
        os_health.CATEGORY_STATUS_FILE = os.path.join(self.tmp, "status.json")
        os_health.HEALTH_FILE = os.path.join(self.tmp, "health")
        os_health.PING_URLS = {"SECURITY": "", "REBOOT": "", "KERNEL": ""}  # no network

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(os_health, k, v)

    def _run_kernel(self):
        with self.assertRaises(SystemExit):  # main() always sys.exit()s
            os_health.main(["KERNEL"])

    def test_preserves_persisted_reboot_fail(self):
        with open(os_health.CATEGORY_STATUS_FILE, "w") as f:
            json.dump({"SECURITY": "ok", "REBOOT": "fail", "KERNEL": "ok"}, f)
        self._run_kernel()
        with open(os_health.HEALTH_FILE) as f:
            self.assertTrue(f.read().startswith("fail "))  # reboot fail still surfaced
        with open(os_health.CATEGORY_STATUS_FILE) as f:
            st = json.load(f)
        self.assertEqual(st["REBOOT"], "fail")  # untouched by kernel-only run
        self.assertEqual(st["KERNEL"], "ok")    # refreshed

    def test_all_ok_when_no_prior_failure(self):
        self._run_kernel()
        with open(os_health.HEALTH_FILE) as f:
            self.assertTrue(f.read().startswith("ok "))


if __name__ == "__main__":
    unittest.main()

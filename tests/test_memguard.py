import unittest
from unittest import mock

import app


class MemGuard(unittest.TestCase):
    def test_rss_read(self):
        self.assertGreater(app.rss_mb(), 0)

    def test_step_trims_and_compares(self):
        trimmed, logs = [], []
        with mock.patch.object(app, "rss_mb", return_value=450):
            self.assertTrue(app.mem_step(430, trimmed.append, log=lambda *a, **k: logs.append(a[0])))
        with mock.patch.object(app, "rss_mb", return_value=300):
            self.assertFalse(app.mem_step(430, None, log=lambda *a, **k: logs.append(a[0])))
        self.assertEqual(trimmed, [0])
        self.assertIn("rss=450", logs[0])

    def test_trim_error_ignored(self):
        def bad(_):
            raise OSError
        with mock.patch.object(app, "rss_mb", return_value=10):
            self.assertFalse(app.mem_step(430, bad, log=lambda *a, **k: None))


if __name__ == "__main__":
    unittest.main()

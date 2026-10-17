"""«SLOW <путь> <мс>» в лог для медленных запросов системы AMURA (/admin/api, дольше 3 с); быстрые и сайт — без записи."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402


class SlowLog(unittest.TestCase):
    def run_req(self, path, slow):
        old = A.SLOW_SEC
        A.SLOW_SEC = -1 if slow else 1000
        self.addCleanup(lambda: setattr(A, "SLOW_SEC", old))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.app.test_client().get(path)
        return out.getvalue()

    def test_slow_logged(self):
        self.assertRegex(self.run_req("/admin/api/me", True), r"SLOW GET /admin/api/me \d+ мс \(401\)")
        self.assertNotIn("SLOW", self.run_req("/admin/api/me", False))
        self.assertNotIn("SLOW", self.run_req("/", True))                       # не система AMURA — не пишем


if __name__ == "__main__":
    unittest.main()

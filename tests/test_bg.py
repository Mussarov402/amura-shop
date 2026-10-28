"""Фоновые потоки — только в воркере gunicorn: в мастере (AMURA_MASTER_PID = свой pid) поток откладывается до post_fork."""
import os
import threading
import unittest
from unittest import mock

import bg


class BG(unittest.TestCase):
    def test_master_defers_worker_runs(self):
        done = threading.Event()
        with mock.patch.dict(os.environ, {"AMURA_MASTER_PID": str(os.getpid())}):
            self.assertTrue(bg.in_master())
            bg.start(done.set)
            self.assertFalse(done.wait(0.2))                      # в мастере не запустился
        self.assertFalse(bg.in_master())                         # после fork pid другой — это воркер
        self.assertEqual(bg.run_pending(), 1)
        self.assertTrue(done.wait(2))
        self.assertEqual(bg.run_pending(), 0)

    def test_without_gunicorn_starts_now(self):
        done = threading.Event()
        with mock.patch.dict(os.environ, {"AMURA_MASTER_PID": ""}):
            bg.start(done.set)
        self.assertTrue(done.wait(2))


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from unittest import mock

import app


class MemGuard(unittest.TestCase):
    def setUp(self):
        self.cg = tempfile.mkdtemp()
        self.patch = mock.patch.object(app, "CGROUP", self.cg)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def put(self, name, text):
        os.makedirs(os.path.dirname(os.path.join(self.cg, name)), exist_ok=True)
        with open(os.path.join(self.cg, name), "w") as f:
            f.write(text)

    def test_rss_and_procs(self):
        self.assertGreater(app.rss_mb(), 0)
        self.assertTrue(any(pid == os.getpid() for _, pid, _ in app.top_procs(50)))

    def test_cgroup_v2(self):
        M = 1 << 20
        self.put("memory.current", str(400 * M))
        self.put("memory.stat", f"anon {150 * M}\nfile {240 * M}\nshmem {30 * M}\ninactive_file {200 * M}\nkernel {8 * M}\nsock {1 * M}\n")
        self.assertEqual(app.cgroup_mem(), {"total": 400, "work": 200, "anon": 150, "file": 240, "shmem": 30, "kernel": 8, "sock": 1})

    def test_cgroup_v1(self):
        M = 1 << 20
        self.put("memory/memory.usage_in_bytes", str(300 * M))
        self.put("memory/memory.stat", f"total_rss {100 * M}\ntotal_cache {150 * M}\ntotal_inactive_file {120 * M}\n")
        c = app.cgroup_mem()
        self.assertEqual((c["total"], c["work"], c["anon"], c["file"]), (300, 180, 100, 150))
        self.assertEqual(app.cgroup_mem.__module__, "app")

    def test_no_cgroup(self):
        self.assertEqual(app.cgroup_mem(), {})

    def test_step_decides(self):
        trimmed, logs = [], []
        log = lambda *a, **k: logs.append(a[0])
        with mock.patch.object(app, "rss_mb", return_value=450), mock.patch.object(app, "cgroup_mem", return_value={}):
            self.assertEqual(app.mem_step(430, trimmed.append, log=log), "worker")
        cg = {"total": 500, "work": 460, "anon": 400, "file": 60, "shmem": 0, "kernel": 5, "sock": 0}
        with mock.patch.object(app, "rss_mb", return_value=100), mock.patch.object(app, "cgroup_mem", return_value=cg):
            self.assertEqual(app.mem_step(430, None, log=log), "all")               # растёт не воркер — перезапуск сервиса
        with mock.patch.object(app, "rss_mb", return_value=100), mock.patch.object(app, "cgroup_mem", return_value=dict(cg, work=300)):
            self.assertIsNone(app.mem_step(430, None, log=log))                    # файловый кэш ядро отдаст само
        self.assertEqual(trimmed, [0])
        self.assertIn("rss=450", logs[0])
        self.assertIn("контейнер 500 МБ (рабочая 460", logs[1])

    def test_trim_error_ignored(self):
        def bad(_):
            raise OSError
        with mock.patch.object(app, "rss_mb", return_value=10):
            self.assertIsNone(app.mem_step(430, bad, log=lambda *a, **k: None))


if __name__ == "__main__":
    unittest.main()

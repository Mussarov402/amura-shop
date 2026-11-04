"""Копия базы Render: внешний адрес, только чтение, пароль не попадает в ошибки и отчёт."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy"))
import dbcopy  # noqa: E402


class DbCopy(unittest.TestCase):
    def test_env_and_secret(self):
        e = dbcopy.pg_env("postgresql://amura_user:p%40ss@dpg-abc-a/amura_shop_db", external=True)
        self.assertEqual((e["PGHOST"], e["PGUSER"], e["PGPASSWORD"], e["PGDATABASE"], e["PGSSLMODE"]),
                         ("dpg-abc-a.frankfurt-postgres.render.com", "amura_user", "p@ss", "amura_shop_db", "require"))
        r = mock.Mock(returncode=1, stderr='connection to server at "dpg-abc-a.frankfurt-postgres.render.com" failed: password p@ss', stdout="")
        with mock.patch.object(dbcopy.subprocess, "run", return_value=r), self.assertRaises(RuntimeError) as cm:
            dbcopy.run(["pg_dump"], e, e["PGHOST"])
        self.assertNotIn("p@ss", str(cm.exception))
        self.assertNotIn("render.com", str(cm.exception))
        self.assertIn("база Render", str(cm.exception))

    def test_main_read_only(self):
        seen = {}

        def fake(args, env, secret, **kw):
            seen.setdefault(args[0], []).append(env)
            if args[0] == "psql":
                return "users\n" if "-tAc" in args else "users\t5\n"
            open(args[args.index("-f") + 1], "w").write("x") if args[0] == "pg_dump" else None
            return ""
        envs = {dbcopy.ENV: {"RENDER_DATABASE_URL": "postgresql://u:pw@dpg-x-a/db"},
                dbcopy.LOCAL: {"DATABASE_URL": "postgresql://amura:l@127.0.0.1:5432/amura"}}
        with mock.patch.object(dbcopy, "load", side_effect=lambda p: envs[p]), mock.patch.object(dbcopy, "run", side_effect=fake), \
                mock.patch.object(dbcopy.subprocess, "run"), mock.patch.object(dbcopy, "BACKUPS", None):
            text = dbcopy.main()
        self.assertIn("default_transaction_read_only=on", seen["pg_dump"][0]["PGOPTIONS"])
        self.assertIn("расхождений нет", text)
        self.assertNotIn("pw", text)

    def _main(self, final, counts_dst, live=False):
        envs = {dbcopy.ENV: {"RENDER_DATABASE_URL": "postgresql://u:pw@dpg-x-a/db"},
                dbcopy.LOCAL: {"DATABASE_URL": "postgresql://amura:l@127.0.0.1:5432/amura"}}
        dumps = []

        def fake(args, env, secret, **kw):
            if args[0] == "psql":
                if "-tAc" in args:
                    return "users\n"
                return "users\t5\n" if env["PGHOST"] != "127.0.0.1" else f"users\t{counts_dst}\n"
            if args[0] == "pg_dump":
                dumps.append(args[args.index("-f") + 1])
                open(dumps[-1], "w").write("x")
            return ""
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(dbcopy, "load", side_effect=lambda p: envs[p]), mock.patch.object(dbcopy, "run", side_effect=fake), \
                mock.patch.object(dbcopy.subprocess, "run"), mock.patch.object(dbcopy, "BACKUPS", d), \
                mock.patch.object(dbcopy, "LIVE", os.path.join(d, "live")):
            if live:
                open(os.path.join(d, "live"), "w").write("x")
            return dbcopy.main(final=final), dumps

    def test_final_keeps_backup_and_needs_exact_rows(self):
        text, dumps = self._main(True, 5)
        self.assertTrue(text.startswith("ФИНАЛЬНАЯ"))
        self.assertTrue(any("before-final-" in x for x in dumps))      # своя база сохранена перед перезаписью
        with self.assertRaises(RuntimeError):
            self._main(True, 4)                                         # расхождение — переход не принимается
        self.assertIn("расхождений 1", self._main(False, 4)[0])         # обычная копия — только сообщает

    def test_no_copy_in_live(self):
        with self.assertRaises(RuntimeError):
            self._main(True, 5, live=True)


if __name__ == "__main__":
    unittest.main()

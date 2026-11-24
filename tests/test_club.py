import sqlite3
import unittest
import club

class DB:
    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.cur = self.c.cursor()
    def run(self, sql, args=(), one=False):
        self.cur.execute(sql.replace("%s", "?"), args)
        if one:
            return self.cur.fetchone()

class ClubTests(unittest.TestCase):
    def setUp(self):
        self.d = DB()
    def tearDown(self):
        self.d.c.close()
    def test_defaults(self):
        self.assertEqual(club.settings(self.d)["target"], 10)
    def test_change_and_restore(self):
        a = club.save(self.d, {**club.DEFAULT, "target": 15}, "owner", "")
        b = club.save(self.d, {**club.DEFAULT, "target": 12}, "owner", a["version"])
        c = club.restore(self.d, a["version"], "owner", b["version"])
        self.assertEqual(c["target"], 15)
        self.assertEqual(club.restore(self.d, "", "owner", c["version"])["target"], 10)
    def test_stale_version(self):
        club.save(self.d, club.DEFAULT, "owner", "")
        with self.assertRaises(ValueError):
            club.save(self.d, club.DEFAULT, "owner", "")
    def test_invalid(self):
        for value in (0, -1, True, 10.5, "10", 101):
            with self.assertRaises(ValueError):
                club.validate({**club.DEFAULT, "target": value})

if __name__ == "__main__":
    unittest.main()

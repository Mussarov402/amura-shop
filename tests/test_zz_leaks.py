"""Идёт последним: тесты не должны оставлять подменённым общий time.sleep — иначе фоновые потоки сервиса
(каталог order_hook, сверки маркетплейсов) крутятся без пауз и полный прогон зависает (31.10.2026, test_estimate)."""
import time
import unittest


class NoLeaks(unittest.TestCase):
    def test_time_sleep_is_real(self):
        self.assertEqual((type(time.sleep).__name__, getattr(time.sleep, "__module__", None)), ("builtin_function_or_method", "time"),
                         "какой-то тест подменил time.sleep и не вернул — подменяйте через mock.patch / addCleanup")


if __name__ == "__main__":
    unittest.main()

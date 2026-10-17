"""ИИ по каналам: выключенный канал — диалог сразу менеджеру."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

import inbox  # noqa: E402


class AiChannelsTest(unittest.TestCase):
    def test_per_channel(self):
        inbox.OPENAI_KEY = "k"
        with inbox.db() as d:
            inbox.set_setting(d, "ai_enabled", "1")
            inbox.set_setting(d, "ai_ch_web", "0")
            self.assertTrue(inbox.ai_on(d))
            self.assertFalse(inbox.ai_on(d, "web:abc12345"))
            self.assertTrue(inbox.ai_on(d, "123456789"))               # Telegram — включён по умолчанию
            self.assertEqual({c["key"]: c["on"] for c in inbox.ai_channels(d)}, {"wa": True, "ig": True, "tg": True, "web": False, "rweb": False})
            inbox.set_setting(d, "ai_enabled", "0")                      # общий выключатель главнее
            self.assertFalse(inbox.ai_on(d, "123456789"))
            inbox.set_setting(d, "ai_enabled", "1")

    def test_retail_support(self):
        """Поддержка amura.kz — свой токен «r-…», ИИ по умолчанию выключен (отвечает менеджер), чат opt.amura.kz не задевает."""
        import webchat
        inbox.OPENAI_KEY = "k"
        tok = webchat.new_token(retail=True)
        chat = webchat.chat_of(tok)
        self.assertTrue(chat.startswith("web:r-") and webchat.is_retail(chat))
        self.assertFalse(webchat.is_retail(webchat.chat_of(webchat.new_token())))
        self.assertIsNone(webchat.chat_of("r-" + tok.split(".")[0][2:] + ".x"))     # подпись проверяется
        self.assertEqual((inbox.channel(chat), inbox.channel_name(chat)), ("web", "Поддержка amura.kz"))
        with inbox.db() as d:
            inbox.set_setting(d, "ai_enabled", "1")
            inbox.set_setting(d, "ai_ch_web", "1")
            self.assertTrue(inbox.ai_on(d, "web:abc12345"))
            self.assertFalse(inbox.ai_on(d, chat))
            inbox.set_setting(d, "ai_ch_rweb", "1")                    # владелец включил ИИ в «Настройках ИИ»
            self.assertTrue(inbox.ai_on(d, chat))
            inbox.set_setting(d, "ai_ch_rweb", "0")


if __name__ == "__main__":
    unittest.main()

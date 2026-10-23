import sys
import unittest
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from family_dates_bot import Event, User, check_and_send_daily_notifications, TelegramBadRequest

class NotificationsTest(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_uses_event_chat_and_all_offsets(self):
        today = date(2026, 10, 16)
        for chat_id in (1234, -1001234):
            for offset in (30, 21, 14, 7, 1):
                with self.subTest(chat_id=chat_id, offset=offset):
                    event = Event(id=1, user_id=1, chat_id=chat_id, title='Anniversary',
                                  event_date=today + timedelta(days=offset), has_birth_year=False,
                                  notifications_enabled=True, gift_title='Flowers')
                    user = User(id=1, telegram_id=1234)
                    rows = MagicMock()
                    rows.all.return_value = [(event, user)]
                    no_log = MagicMock()
                    no_log.scalar_one_or_none.return_value = None
                    session = AsyncMock()
                    session.execute.side_effect = [rows, no_log]
                    session.add = MagicMock()
                    session.__aenter__.return_value = session
                    maker = MagicMock(return_value=session)
                    bot = AsyncMock()
                    count = await check_and_send_daily_notifications(bot, maker, today)
                    self.assertEqual(count, 1)
                    self.assertEqual(bot.send_message.await_args.kwargs['chat_id'], chat_id)
                    session.commit.assert_awaited_once()

    async def test_broken_saved_photo_falls_back_to_text(self):
        today = date(2026, 10, 23)
        event = Event(id=1, user_id=1, chat_id=-1001234, title='Birthday',
                      event_date=today + timedelta(days=1), has_birth_year=False,
                      notifications_enabled=True, gift_title='Cup', gift_photo_file_id='expired-file')
        rows, no_log = MagicMock(), MagicMock()
        rows.all.return_value = [(event, User(id=1, telegram_id=1234))]
        no_log.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute.side_effect = [rows, no_log]
        session.add = MagicMock()
        session.__aenter__.return_value = session
        bot = AsyncMock()
        bot.send_photo.side_effect = TelegramBadRequest(method='sendPhoto', message='bad photo')
        self.assertEqual(await check_and_send_daily_notifications(bot, MagicMock(return_value=session), today), 1)
        self.assertEqual(bot.send_message.await_args.kwargs['chat_id'], event.chat_id)
        session.commit.assert_awaited_once()

if __name__ == '__main__':
    unittest.main()

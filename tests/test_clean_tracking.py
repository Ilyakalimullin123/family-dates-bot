import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiogram.methods import SendMessage
from aiogram.types import Message, Chat
from family_dates_bot import TrackInterfaceMessages, preserve_messages

class TrackingTest(unittest.IsolatedAsyncioTestCase):
    async def test_interface_tracked_but_reminder_preserved(self):
        session = AsyncMock()
        session.__aenter__.return_value = session
        middleware = TrackInterfaceMessages(MagicMock(return_value=session))
        result = Message(message_id=77, date=datetime.now(timezone.utc),
                         chat=Chat(id=-100123, type='supergroup'), text='Menu')
        request = AsyncMock(return_value=result)
        method = SendMessage(chat_id=-100123, text='Menu')
        await middleware(request, MagicMock(), method)
        record = session.merge.await_args.args[0]
        self.assertEqual((record.chat_id, record.message_id), (-100123, 77))
        session.merge.reset_mock()
        token = preserve_messages.set(True)
        try:
            await middleware(request, MagicMock(), method)
        finally:
            preserve_messages.reset(token)
        session.merge.assert_not_awaited()


class IncomingTrackingTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_message_saved_before_handler(self):
        from aiogram.types import User
        from family_dates_bot import TrackPrivateUserMessages
        session = AsyncMock()
        session.__aenter__.return_value = session
        middleware = TrackPrivateUserMessages(MagicMock(return_value=session))
        message = Message(message_id=19, date=datetime.now(timezone.utc),
                          chat=Chat(id=42, type='private'),
                          from_user=User(id=42, is_bot=False, first_name='User'), text='hello')
        async def handle(event, data):
            session.commit.assert_awaited_once()
            return 'handled'
        self.assertEqual(await middleware(handle, message, {}), 'handled')
        record = session.merge.await_args.args[0]
        self.assertEqual((record.chat_id, record.message_id), (42, 19))

    async def test_group_conversation_not_registered(self):
        from aiogram.types import User
        from family_dates_bot import TrackPrivateUserMessages
        maker = MagicMock()
        middleware = TrackPrivateUserMessages(maker)
        for chat_type in ('group', 'supergroup'):
            message = Message(message_id=19, date=datetime.now(timezone.utc),
                              chat=Chat(id=-42, type=chat_type),
                              from_user=User(id=42, is_bot=False, first_name='User'), text='hello')
            handler = AsyncMock()
            await middleware(handler, message, {})
            handler.assert_awaited_once_with(message, {})
        maker.assert_not_called()

    async def test_storage_failure_does_not_block_command(self):
        from aiogram.types import User
        from family_dates_bot import TrackPrivateUserMessages
        middleware = TrackPrivateUserMessages(MagicMock(side_effect=RuntimeError('offline')))
        message = Message(message_id=19, date=datetime.now(timezone.utc),
                          chat=Chat(id=42, type='private'),
                          from_user=User(id=42, is_bot=False, first_name='User'), text='/clean')
        handler = AsyncMock(return_value='ok')
        with self.assertLogs('family_dates_bot', level='ERROR'):
            self.assertEqual(await middleware(handler, message, {}), 'ok')
        handler.assert_awaited_once()

    async def test_clean_deletes_registered_incoming_message_and_shows_menu(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from family_dates_bot import cmd_clean, InterfaceMessage
        session = AsyncMock()
        session.__aenter__.return_value = session
        result = MagicMock()
        record = InterfaceMessage(chat_id=42, message_id=19)
        result.scalars.return_value.all.return_value = [record]
        session.execute.return_value = result
        message = SimpleNamespace(chat=SimpleNamespace(id=42), from_user=object(),
                                  bot=SimpleNamespace(delete_message=AsyncMock()), delete=AsyncMock())
        state = AsyncMock()
        state.get_data.return_value = {}
        maker = MagicMock(return_value=session)
        with patch('family_dates_bot.show_events_menu', new=AsyncMock()) as menu:
            await cmd_clean(message, state, maker)
            menu.assert_awaited_once_with(message, message.from_user, maker)
        message.bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=19)
        session.delete.assert_awaited_once_with(record)
        message.delete.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

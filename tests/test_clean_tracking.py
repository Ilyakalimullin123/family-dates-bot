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

if __name__ == '__main__':
    unittest.main()

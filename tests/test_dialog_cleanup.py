import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import DeleteMessage
from family_dates_bot import clean_dialog, is_dialog_reply

class CleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_recorded_messages_deleted_even_without_permission(self):
        state = AsyncMock()
        state.get_data.return_value = {'dialog_message_ids': [10, 12]}
        bot = AsyncMock()
        bot.delete_message.side_effect = [TelegramForbiddenError(
            method=DeleteMessage(chat_id=-100, message_id=10), message='Forbidden'), True]
        message = SimpleNamespace(chat=SimpleNamespace(id=-100), bot=bot)
        await clean_dialog(message, state)
        self.assertEqual([call.kwargs['message_id'] for call in bot.delete_message.await_args_list], [10, 12])
        state.clear.assert_awaited_once()

    async def test_group_chatter_is_not_a_dialog_reply(self):
        state = AsyncMock()
        state.get_data.return_value = {'dialog_prompt_id': 42}
        message = SimpleNamespace(chat=SimpleNamespace(type='supergroup'),
                                  text='обычная переписка', reply_to_message=None)
        self.assertFalse(await is_dialog_reply(message, state))
        message.reply_to_message = SimpleNamespace(message_id=41)
        self.assertFalse(await is_dialog_reply(message, state))
        message.reply_to_message.message_id = 42
        self.assertTrue(await is_dialog_reply(message, state))

if __name__ == '__main__':
    unittest.main()

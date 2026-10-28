import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import growth
import family_dates_bot as app


class GrowthTest(unittest.TestCase):
    def test_sources_validate_and_do_not_store_invitation_secrets(self):
        token = 'a' * 24
        for payload, expected in [('', 'direct'), ('src_family_1', 'family_1'),
                                  ('src_<script>', 'direct'), ('src_' + 'a' * 49, 'direct'),
                                  ('i_' + token, 'invitation'), ('w_' + token, 'wishlist')]:
            self.assertEqual(growth.source_from_payload(payload), expected)

    def test_wish_input(self):
        self.assertEqual(growth.parse_wish('Книга https://example.com/book?q=1'), ('Книга', 'https://example.com/book?q=1'))
        self.assertEqual(growth.parse_wish('Ужин вместе'), ('Ужин вместе', None))
        for value in ['a', 'a' * 201, 'https://example.com', 'Книга https://u:p@example.com',
                      'Книга https://one.test https://two.test', 'Книга https://']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                growth.parse_wish(value)

    def test_owner_output_does_not_leak_reservation(self):
        wish = growth.Wish(id=1, owner_id=10, title='<Книга>', url='https://example.com/?a=1&b=2')
        free = growth.wish_view(wish, 10, 'token')
        wish.reserved_by = 20
        reserved = growth.wish_view(wish, 10, 'token')
        self.assertEqual(free, reserved)
        self.assertIn('&lt;Книга&gt;', free[0])
        self.assertNotIn('20', str(reserved))
        self.assertIn('Ты выбрал', growth.wish_view(wish, 20, 'token')[0])
        self.assertIn('другой человек', growth.wish_view(wish, 30, 'token')[0])

    def test_admin_default_closed_and_never_in_groups(self):
        from unittest.mock import patch
        message = SimpleNamespace(chat=SimpleNamespace(type='private'), from_user=SimpleNamespace(id=42))
        with patch.dict('os.environ', {'BOT_ADMIN_IDS': ''}):
            self.assertFalse(app.growth_features.is_admin(message))
        with patch.dict('os.environ', {'BOT_ADMIN_IDS': '42, 55'}):
            self.assertTrue(app.growth_features.is_admin(message))
            message.chat.type = 'group'
            self.assertFalse(app.growth_features.is_admin(message))


class GrowthFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_confirmation_cannot_submit_new_form(self):
        state = AsyncMock()
        state.get_state.return_value = growth.Flow.confirm.state
        state.get_data.return_value = {'nonce': 'new'}
        callback = SimpleNamespace(answer=AsyncMock())
        await app.growth_features.submit(callback, state, None, 'old')
        self.assertIn('устарело', callback.answer.await_args.args[0])

    async def test_group_cannot_use_booking_buttons(self):
        callback = SimpleNamespace(data='gx_reserve:1:token', from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(chat=SimpleNamespace(type='group')), answer=AsyncMock())
        await app.growth_features.callback(callback, AsyncMock(), None)
        self.assertIn('личный чат', callback.answer.await_args.args[0])

    async def test_cancel_clears_dialog(self):
        message, state = AsyncMock(), AsyncMock()
        await app.growth_features.cancel(message, state)
        state.clear.assert_awaited_once()


class RoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_reaches_growth_router_from_legacy_dialogs(self):
        from aiogram import Bot, Dispatcher
        from aiogram.types import Message, Update
        from unittest.mock import patch
        dp = Dispatcher()
        dp.include_router(app.router)
        bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        try:
            state = dp.fsm.get_context(bot=bot, chat_id=42, user_id=42)
            for previous in (app.AddEventStates.waiting_for_title, app.AddEventStates.waiting_for_date,
                             app.AttachGiftStates.waiting_for_gift_data, growth.Flow.name, growth.Flow.confirm):
                await state.set_state(previous)
                update = Update.model_validate({'update_id': 1, 'message': {'message_id': 1, 'date': 1,
                    'chat': {'id': 42, 'type': 'private'}, 'from': {'id': 42, 'is_bot': False, 'first_name': 'Alice'},
                    'text': '/cancel', 'entities': [{'type': 'bot_command', 'offset': 0, 'length': 7}]}})
                with patch.object(Message, 'answer', new=AsyncMock()) as answer:
                    await dp.feed_update(bot, update)
                    self.assertIsNone(await state.get_state())
                    answer.assert_awaited_once()
        finally:
            await dp.storage.close()
            await bot.session.close()

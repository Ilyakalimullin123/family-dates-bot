import sys
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import family_dates_bot as app

class GiftFeedbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_dislike_saves_feedback_and_immediately_refreshes(self):
        event = app.Event(id=7, title='Birthday', event_date=date(2000, 1, 1),
                          has_birth_year=True, recipient_gender='male', recipient_tags=[])
        gift = app.GiftCatalog(id=11, title='Gift', category='Test', times_rejected=0, is_approved=True)
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.get.return_value = gift
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute.return_value = result
        session.add = MagicMock()
        callback = AsyncMock()
        callback.message.photo = None
        callback.data = 'gift_fb:7:11:0'
        replacement = [(app.GiftCatalog(id=12, title='Next'), 3.0)]
        with patch.object(app, 'get_chat_event', AsyncMock(return_value=event)), \
             patch.object(app, 'get_or_create_user', AsyncMock(return_value=app.User(id=1))), \
             patch.object(app, 'get_smart_recommendations', AsyncMock(return_value=replacement)) as rank, \
             patch.object(app, 'show_gift_recommendations', AsyncMock()) as display:
            await app.cb_gift_feedback(callback, MagicMock(return_value=session))
            session.commit.assert_awaited_once()
            self.assertEqual(gift.times_rejected, 1)
            self.assertEqual(session.add.call_args.args[0].interaction_type, 'recommended_rejected')
            rank.assert_awaited_once()
            self.assertEqual(display.await_args.args[3], replacement)

    async def test_exhausted_catalog_has_next_actions(self):
        callback = AsyncMock()
        callback.message.photo = None
        await app.show_gift_recommendations(callback, 7, 'Birthday', [])
        keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
        actions = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn('gift_preferences:7', actions)
        self.assertIn('attach_gift:7', actions)
        self.assertIn('show_event:7', actions)

if __name__ == '__main__':
    unittest.main()

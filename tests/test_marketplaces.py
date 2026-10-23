import unittest
from unittest.mock import AsyncMock
import family_dates_bot as app
from scripts.import_gifts import validate


class MarketplaceTests(unittest.IsolatedAsyncioTestCase):
    def test_search_encoding(self):
        links = app.marketplace_search_links('чай & кофе')
        self.assertEqual(len(links), 3)
        self.assertTrue(all('%26' in url and ' ' not in url for _, url in links))

    def test_import_validation(self):
        product = dict(title='Термос', category='Туризм', shop_url='https://kaspi.kz/shop/p/example-123/', image_url='https://resources.cdn-kaspi.kz/photo.jpg', price_checked_on='2026-10-23')
        self.assertEqual(validate([product])[0]['title'], 'Термос')
        for url in ['https://kaspi.kz.evil.test/p/1', 'http://ozon.ru/product/1', 'https://ozon.ru/search/?text=x']:
            with self.assertRaises(ValueError):
                validate([dict(product, shop_url=url)])
        with self.assertRaises(ValueError):
            validate([product, product])

    async def test_broken_photo_keeps_actions(self):
        callback = AsyncMock()
        callback.message.photo = None
        callback.message.answer_photo.side_effect = app.TelegramBadRequest(method='sendPhoto', message='bad photo')
        gift = app.GiftCatalog(id=1, title='Термос', category='Туризм', tags=[], image_url='https://example.com/photo.jpg')
        await app.show_gift_recommendations(callback, 1, 'День рождения', [(gift, 2)])
        callback.message.edit_text.assert_awaited_once()
        buttons = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertTrue(any(b.callback_data == 'gift_fb:1:1:0' for row in buttons for b in row))

    async def test_photo_caption_contains_actions_without_second_message(self):
        callback = AsyncMock()
        gift = app.GiftCatalog(id=1, title='Термос', category='Туризм', tags=[], image_url='https://example.com/photo.jpg')
        await app.show_gift_recommendations(callback, 1, 'День рождения', [(gift, 2)])
        callback.message.answer_photo.assert_awaited_once()
        callback.message.answer.assert_not_awaited()
        sent = callback.message.answer_photo.await_args.kwargs
        self.assertLessEqual(len(sent["caption"]), 1024)
        self.assertIn("gift_fb:1:1:0", [b.callback_data for row in sent["reply_markup"].inline_keyboard for b in row])
        callback.message.edit_text.assert_not_awaited()

class PhotoNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_to_settings_disables_old_buttons(self):
        callback = AsyncMock()
        callback.message.photo = [object()]
        await app.edit_gift_message(callback, 'Бюджет', app.budget_keyboard(1))
        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        callback.message.edit_text.assert_not_awaited()
        callback.message.answer.assert_awaited_once()

    def test_catalog_has_only_real_products_with_unique_urls(self):
        import json
        from pathlib import Path
        items = validate(json.loads((Path(__file__).resolve().parents[1] / 'catalog/products.json').read_text()))
        self.assertGreaterEqual(len(items), 18)
        self.assertTrue(all(item['verified_product'] and item['image_url'] for item in items))

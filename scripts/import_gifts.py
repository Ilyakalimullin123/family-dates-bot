"""Import a reviewed JSON catalogue. Run from repository root with --apply to save."""
import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import family_dates_bot as app

HOSTS = {'kaspi.kz', 'www.wildberries.ru', 'wildberries.ru', 'www.wildberries.kz',
         'wildberries.kz', 'ozon.ru', 'www.ozon.ru', 'ozon.kz', 'www.ozon.kz'}


def validate(items):
    if not isinstance(items, list):
        raise ValueError('Expected a JSON array')
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Each product must be an object')
        title, category = item.get('title', ''), item.get('category', '')
        url = item.get('shop_url', '')
        parsed = urlsplit(url)
        if not isinstance(title, str) or not 1 <= len(title) <= 255:
            raise ValueError('Invalid title')
        if not isinstance(category, str) or not 1 <= len(category) <= 100:
            raise ValueError('Invalid category')
        if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port:
            raise ValueError('shop_url must be an HTTPS marketplace URL')
        if parsed.path in ('', '/') or 'search' in parsed.path.lower():
            raise ValueError('Use a product URL, not a homepage or search URL')
        if url in seen:
            raise ValueError('Duplicate shop_url')
        seen.add(url)
        image = item.get('image_url')
        if not image or urlsplit(image).scheme != 'https' or not urlsplit(image).hostname:
            raise ValueError('image_url must use HTTPS')
        tags = item.get('tags', [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or len(t) > 40 for t in tags):
            raise ValueError('Invalid tags')
        minimum = item.get('estimated_min_kzt')
        if minimum is not None and (type(minimum) is not int or minimum <= 0):
            raise ValueError('Invalid estimated_min_kzt')
        checked = date.fromisoformat(item['price_checked_on'])
        min_age, max_age = item.get('min_age', 14), item.get('max_age', 120)
        if type(min_age) is not int or type(max_age) is not int or not 0 <= min_age <= max_age <= 120:
            raise ValueError('Invalid age range')
        result.append(dict(price_checked_on=checked, verified_product=True, min_age=min_age, max_age=max_age, title=title, category=category, shop_url=url, image_url=image,
                           tags=tags, estimated_min_kzt=minimum))
    return result


async def save(items):
    engine = app.create_async_engine(app.DATABASE_URL, pool_pre_ping=True)
    try:
        maker = app.async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            for item in items:
                gift = (await session.execute(app.select(app.GiftCatalog).where(
                    app.GiftCatalog.shop_url == item['shop_url'],
                    app.GiftCatalog.created_by_user_id.is_(None)
                ))).scalar_one_or_none()
                if gift is None:
                    gift = app.GiftCatalog(target_gender='any', target_relations=[],
                                          min_age=0, max_age=120, base_score=2.0, is_approved=True)
                    session.add(gift)
                if gift.image_url != item["image_url"]:
                    gift.image_file_id = None
                for key, value in item.items():
                    setattr(gift, key, value)
            await session.commit()
    finally:
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    items = validate(json.loads(args.file.read_text()))
    if args.apply:
        asyncio.run(save(items))
    print(f'{len(items)} products: ' + ('saved' if args.apply else 'validated; use --apply to save'))

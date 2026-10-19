"""PostgreSQL integration checks in a temporary schema rolled back on exit."""
import asyncio
import sys
from pathlib import Path
from datetime import date
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import family_dates_bot as app
from sqlalchemy import text

async def main():
    engine = app.create_async_engine(app.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                schema = 'test_recs_' + uuid4().hex
                await conn.execute(text(f'CREATE SCHEMA {schema}'))
                await conn.execute(text(f'SET LOCAL search_path TO {schema}'))
                await conn.run_sync(app.Base.metadata.create_all)
                async with app.AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as session:
                    user = app.User(telegram_id=123)
                    session.add(user)
                    await session.flush()
                    event = app.Event(user_id=user.id, chat_id=-123, title='Test', event_date=date(2000,1,1),
                                      recipient_gender='female', recipient_tags=['туризм'], gift_budget_kzt=10000)
                    other = app.Event(user_id=user.id, chat_id=-456, title='Other', event_date=date(2000,1,1),
                                      recipient_gender='female', recipient_tags=['туризм'], gift_budget_kzt=10000)
                    gifts = [app.GiftCatalog(title=str(i), category=category, target_gender=gender,
                              tags=tags, estimated_min_kzt=price, base_score=2.0)
                             for i, (category, gender, tags, price) in enumerate([
                                 ('Outdoors', 'male', ['туризм'], 5000),
                                 ('Books', 'any', ['книги'], 4000),
                                 ('Luxury', 'any', ['туризм'], 50000),
                                 ('Unknown', 'any', ['туризм'], None)])]
                    session.add_all([event, other, *gifts])
                    await session.flush()
                    result = await app.get_smart_recommendations(session, event, 26)
                    assert [g.id for g, _ in result] == [gifts[0].id, gifts[1].id]
                    session.add(app.GiftMLDataset(user_id=user.id, event_id=event.id, gift_id=gifts[0].id,
                        recipient_gender='female', gift_title='0', interaction_type='recommended_rejected',
                        rejection_reason='already_have', label=0))
                    await session.flush()
                    assert [g.id for g, _ in await app.get_smart_recommendations(session, event, 26)] == [gifts[1].id]
                    assert gifts[0].id in [g.id for g, _ in await app.get_smart_recommendations(session, other, 26)]
                    await session.commit()
                    callback = SimpleNamespace(
                        message=SimpleNamespace(photo=None, chat=SimpleNamespace(id=-123), edit_text=AsyncMock()),
                        from_user=SimpleNamespace(id=123))
                    maker = app.async_sessionmaker(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint')
                    recommendations = await app.get_smart_recommendations(session, event, 26)
                    await app.show_gift_recommendations(callback, event.id, event.title, recommendations, maker)
                    impressions = (await session.execute(app.select(app.GiftImpression))).scalars().all()
                    assert len(impressions) == 1 and impressions[0].budget_kzt == 10000
                    record = (await session.execute(app.select(app.GiftMLDataset))).scalars().one()
                    assert record.label == 0  # Viewing a replacement never creates a new rejection.
                    print('PASS: shown items record position and budget without implicit dislike')
                    print('PASS: budget, unknown price exclusion, interest over gender, rejection isolation')
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()

asyncio.run(main())

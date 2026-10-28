"""End-to-end database checks in a temporary PostgreSQL schema; all data rolled back."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import family_dates_bot as app
import growth as g
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import text


def actor(uid):
    return NS(id=uid, first_name=f'Person {uid}', username=None)


def message(uid, value='', chat_id=None):
    return NS(text=value, chat=NS(id=chat_id or uid, type='private' if (chat_id or uid) > 0 else 'supergroup', title='Family'),
              from_user=actor(uid), answer=AsyncMock(), edit_reply_markup=AsyncMock(), bot=AsyncMock())


def callback(uid, value, msg=None):
    msg = msg or message(uid)
    return NS(data=value, from_user=actor(uid), message=msg, answer=AsyncMock(), bot=msg.bot)


async def main():
    engine = app.create_async_engine(app.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                schema = 'test_growth_' + uuid4().hex
                await conn.execute(text(f'CREATE SCHEMA {schema}'))
                await conn.execute(text(f'SET LOCAL search_path TO {schema}'))
                await conn.run_sync(app.Base.metadata.create_all)
                await conn.run_sync(g.GrowthBase.metadata.create_all)
                # Schema creation must remain idempotent.
                await conn.run_sync(g.GrowthBase.metadata.create_all)
                maker = app.async_sessionmaker(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint')
                storage = MemoryStorage()
                state = FSMContext(storage, StorageKey(bot_id=1, chat_id=20, user_id=20))
                features = app.growth_features
                token, wishlist_token = 'i' * 24, 'w' * 24
                async with maker() as session:
                    session.add(g.Invitation(token=token, owner_id=10, chat_id=10, destination='Family list',
                                             expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
                    session.add(g.Wishlist(owner_id=10, token=wishlist_token, name='Owner'))
                    await session.flush()
                    session.add(g.Wish(owner_id=10, title='Book'))
                    await session.commit()
                start = message(20, '/start i_' + token)
                assert await features.start(start, state, maker)
                await features.callback(callback(20, 'gx_join:' + token), state, maker)
                await features.input(message(20, 'Alice'), state, maker)
                await features.input(message(20, '29.02'), state, maker)
                assert await state.get_state() == g.Flow.confirm.state
                nonce = (await state.get_data())['nonce']
                confirm = callback(20, 'gx_confirm:' + nonce)
                await features.callback(confirm, state, maker)
                assert await state.get_state() is None
                async with maker() as session:
                    event = (await session.execute(app.select(app.Event))).scalar_one()
                    assert event.chat_id == 10 and event.title == 'День рождения Alice'
                    assert event.event_date.month == 2 and event.event_date.day == 29
                    assert event.notifications_enabled and not event.has_birth_year
                    assert not (await session.execute(app.select(app.Event).where(app.Event.chat_id == 20))).scalars().all()
                # Reopening and confirming the same destination never duplicates the event.
                await features.callback(callback(20, 'gx_join:' + token), state, maker)
                await features.input(message(20, 'Alice again'), state, maker)
                await features.input(message(20, '01.01.2000'), state, maker)
                duplicate = callback(20, 'gx_confirm:' + (await state.get_data())['nonce'])
                await features.callback(duplicate, state, maker)
                assert 'уже передал' in duplicate.answer.await_args.args[0]
                async with maker() as session:
                    assert await session.scalar(app.select(app.func.count()).select_from(app.Event)) == 1
                # Revoking between the date prompt and confirmation cancels the write.
                other_state = FSMContext(storage, StorageKey(bot_id=1, chat_id=30, user_id=30))
                await features.callback(callback(30, 'gx_join:' + token), other_state, maker)
                await features.input(message(30, 'Bob'), other_state, maker)
                await features.input(message(30, '01.01.2000'), other_state, maker)
                pending_nonce = (await other_state.get_data())['nonce']
                await features.callback(callback(30, 'gx_revoke:' + token), other_state, maker)
                async with maker() as session:
                    assert await session.get(g.Invitation, token) is not None  # Nonowner blocked.
                await features.callback(callback(10, 'gx_revoke:' + token), state, maker)
                revoked = callback(30, 'gx_confirm:' + pending_nonce)
                await features.callback(revoked, other_state, maker)
                assert 'больше не действует' in revoked.answer.await_args.args[0]
                async with maker() as session:
                    assert await session.scalar(app.select(app.func.count()).select_from(app.Event)) == 1
                    wish = (await session.execute(app.select(g.Wish))).scalar_one()
                    wish_id = wish.id
                    assert not await g.reserve_wish(session, wish_id, 'invalid', 20)
                    assert not await g.reserve_wish(session, wish_id, wishlist_token, 10)
                    assert await g.reserve_wish(session, wish_id, wishlist_token, 20)
                    assert not await g.reserve_wish(session, wish_id, wishlist_token, 30)
                    assert not await g.reserve_wish(session, wish_id, wishlist_token, 30, release=True)
                    assert await g.reserve_wish(session, wish_id, wishlist_token, 20, release=True)
                    assert await g.reserve_wish(session, wish_id, wishlist_token, 30)
                    await session.commit()
                # Forged deletion cannot delete another user's wish.
                await features.show_wishlist(message(20), actor(20), maker)
                await features.callback(callback(20, f'gx_delete_confirm:{wish_id}'), state, maker)
                async with maker() as session:
                    assert await session.get(g.Wish, wish_id)
                await features.callback(callback(10, 'gx_rotate:' + wishlist_token), state, maker)
                async with maker() as session:
                    assert not await g.reserve_wish(session, wish_id, wishlist_token, 30, release=True)
                    assert (await session.get(g.Wish, wish_id)).reserved_by == 30
                # First-touch source survives later campaign links.
                await g.record(maker, 20, 'start', 'paid_campaign')
                async with maker() as session:
                    assert (await session.get(g.Acquisition, 20)).source == 'invitation'
                    # Retrying today's reminder must not deliver a successful one twice.
                    from datetime import date
                    event = (await session.execute(app.select(app.Event))).scalar_one()
                    event.event_date = date(2000, 1, 2)
                    await session.commit()
                bot = AsyncMock()
                assert await app.check_and_send_daily_notifications(bot, maker, date(2026, 1, 1)) == 1
                assert await app.check_and_send_daily_notifications(bot, maker, date(2026, 1, 1)) == 0
                assert bot.send_message.await_count == 1
                await storage.close()
                print('PASS: invitation consent, destination isolation, leap birthday, duplicate prevention, revocation, '
                      'reservation ownership/conflicts, wishlist ownership, stale links, first-touch attribution, reminder deduplication')
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

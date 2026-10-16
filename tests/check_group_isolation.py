"""Integration check against configured PostgreSQL; test rows are rolled back."""
import asyncio
import sys
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from family_dates_bot import (
    DATABASE_URL, Event, User, AsyncSession, create_async_engine,
    async_sessionmaker, get_chat_event, init_db_and_seed, select, delete,
)

async def main():
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await init_db_and_seed(engine, maker)
        # Second run checks that the migration is safe to repeat.
        await init_db_and_seed(engine, maker)
        async with AsyncSession(engine) as session:
            user = User(telegram_id=-990001234567, first_name='Group isolation test')
            session.add(user)
            await session.flush()
            events = [Event(user_id=user.id, chat_id=chat_id, title='Test',
                            event_date=date(2000, 1, 1))
                      for chat_id in (990001234567, -990001234568, -990001234569)]
            session.add_all(events)
            await session.flush()
            for event in events:
                for chat_id in (e.chat_id for e in events):
                    found = await get_chat_event(session, event.id, chat_id)
                    assert (found is not None) == (event.chat_id == chat_id)
            result = await session.execute(delete(Event).where(
                Event.id == events[1].id, Event.chat_id == events[2].chat_id
            ).returning(Event.id))
            assert result.scalar_one_or_none() is None
            assert await get_chat_event(session, events[1].id, events[1].chat_id)
            await session.rollback()
        print('PASS: migration repeatability, personal/group isolation, cross-group deletion blocked')
    finally:
        await engine.dispose()

asyncio.run(main())

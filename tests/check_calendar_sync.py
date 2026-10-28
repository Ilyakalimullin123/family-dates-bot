"""Calendar HTTP + PostgreSQL integration. Temporary schema and all data rolled back."""
import asyncio
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text
import calendar_sync as cal
import family_dates_bot as app
import growth


async def main():
    engine = app.create_async_engine(app.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                schema = 'test_calendar_' + uuid4().hex
                await conn.execute(text(f'CREATE SCHEMA {schema}'))
                await conn.execute(text(f'SET LOCAL search_path TO {schema}'))
                for metadata in (app.Base.metadata, growth.GrowthBase.metadata, cal.CalendarBase.metadata):
                    await conn.run_sync(metadata.create_all)
                await conn.run_sync(cal.CalendarBase.metadata.create_all)
                maker = app.async_sessionmaker(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint')
                async with maker() as session:
                    user = app.User(telegram_id=42, first_name='Owner')
                    session.add(user)
                    await session.flush()
                    first = app.Event(user_id=user.id, chat_id=42, title='Private birthday', event_date=date(1984, 5, 12))
                    group = app.Event(user_id=user.id, chat_id=-123, title='Group anniversary', event_date=date(2000, 2, 29))
                    session.add_all([first, group])
                    await session.commit()
                    private = await cal.ensure_feed(session, 42, 'Personal')
                    repeated = await cal.ensure_feed(session, 42, 'Another name')
                    assert private.token == repeated.token
                    shared = await cal.ensure_feed(session, -123, 'Group')
                    private_token, group_token, event_id = private.token, shared.token, first.id
                client = TestClient(TestServer(app.calendar_features.web_app(maker), access_log=None))
                async with client:
                    path = f'/calendar/{private_token}.ics'
                    response = await client.get(path)
                    assert response.status == 200
                    assert response.content_type == 'text/calendar'
                    content = await response.text()
                    etag = response.headers['ETag']
                    assert 'Private birthday' in content and 'Group anniversary' not in content
                    assert 'CLASS:PUBLIC' in content and 'CLASS:PRIVATE' not in content
                    assert '1984' not in content
                    assert (await client.get(path, headers={'If-None-Match': etag})).status == 304
                    assert (await client.head(path)).status == 200
                    assert (await client.post(path)).status == 405
                    assert (await client.get('/calendar/' + 'z' * 43 + '.ics')).status == 404
                    assert (await client.get('/calendar/42.ics')).status == 404
                    # A name/date change updates the feed, not its UID.
                    async with maker() as session:
                        await session.execute(app.update(app.Event).where(app.Event.id == event_id).values(title='Changed birthday'))
                        await session.commit()
                    response = await client.get(path, headers={'If-None-Match': etag})
                    assert response.status == 200 and response.headers['ETag'] != etag
                    revised = await response.text()
                    uid = lambda body: next(line for line in body.split('\r\n') if line.startswith('UID:'))
                    assert uid(content) == uid(revised)
                    assert 'Changed birthday' in revised
                    # Group migration preserves its subscription and excludes personal events.
                    await app.migrate_group(NS(chat=NS(id=-123), migrate_to_chat_id=-100555), maker)
                    response = await client.get(f'/calendar/{group_token}.ics')
                    assert response.status == 200
                    group_body = await response.text()
                    assert 'Group anniversary' in group_body and 'Changed birthday' not in group_body
                    # A different private chat cannot revoke this subscription even with its token.
                    cb = NS(data=f'cal_revoke:{private_token}', from_user=NS(id=99),
                            message=NS(chat=NS(id=99, type='private')), answer=AsyncMock())
                    await app.calendar_features.callback(cb, AsyncMock(), maker)
                    assert (await client.get(path)).status == 200
                    # Deleted events disappear from the feed, including an empty calendar.
                    async with maker() as session:
                        await session.execute(app.delete(app.Event).where(app.Event.id == event_id))
                        await session.commit()
                    response = await client.get(path)
                    assert response.status == 200 and 'BEGIN:VEVENT' not in await response.text()
                    cb.from_user.id = cb.message.chat.id = 42
                    await app.calendar_features.callback(cb, AsyncMock(), maker)
                    assert (await client.get(path, headers={'If-None-Match': etag})).status == 404
                    async with maker() as session:
                        fresh = await cal.ensure_feed(session, 42, 'Personal')
                        assert fresh.token != private_token
                        fresh_token = fresh.token
                    assert (await client.get(f'/calendar/{fresh_token}.ics')).status == 200
                    # A stale confirmation cannot revoke the replacement link.
                    await app.calendar_features.callback(cb, AsyncMock(), maker)
                    assert (await client.get(f'/calendar/{fresh_token}.ics')).status == 200
                print('PASS: HTTP calendar/ETag, per-chat isolation, stable UID and link, edits/deletion, '
                      'group migration, revocation ownership, revoked and stale tokens')
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

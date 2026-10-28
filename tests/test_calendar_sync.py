import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calendar_sync as cal
import family_dates_bot as app


def event(**changes):
    fields = dict(id=1, title='День рождения мамы', event_date=date(1984, 5, 12),
                  created_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
                  updated_at=datetime(2026, 10, 28, 10, tzinfo=timezone.utc),
                  gift_title='Secret surprise', gift_url='https://secret.example.com')
    return NS(**(fields | changes))


def unfold(value):
    return value.decode('utf-8').replace('\r\n ', '')


class CalendarFormatTest(unittest.TestCase):
    def test_subscription_visible_to_readers_but_file_import_stays_private(self):
        value = unfold(cal.render_calendar([event()], for_subscription=True))
        self.assertIn('CLASS:PUBLIC\r\n', value)
        self.assertNotIn('CLASS:PRIVATE', value)
        self.assertIn('SEQUENCE:', value)
        self.assertIn('CLASS:PRIVATE\r\n', unfold(cal.render_calendar([event()])))

    def test_all_day_yearly_no_birth_year_or_gifts(self):
        value = unfold(cal.render_calendar([event()]))
        self.assertIn('DTSTART;VALUE=DATE:20260512\r\n', value)
        self.assertIn('DTEND;VALUE=DATE:20260513\r\n', value)
        self.assertIn('RRULE:FREQ=YEARLY\r\n', value)
        self.assertNotIn('1984', value)
        self.assertNotIn('Secret surprise', value)
        self.assertNotIn('secret.example.com', value)
        self.assertNotIn('VALARM', value)
        self.assertTrue(value.endswith('END:VCALENDAR\r\n'))

    def test_uid_stable_when_date_title_or_feed_name_changes(self):
        import re
        first = unfold(cal.render_calendar([event()], 'Name', 'bot-1'))
        second = unfold(cal.render_calendar([event(title='Changed', event_date=date(2000, 6, 2))], 'New', 'bot-1'))
        uid = lambda value: re.search(r'^UID:(.*)$', value, re.M).group(1)
        self.assertEqual(uid(first), uid(second))
        self.assertNotEqual(uid(first), uid(unfold(cal.render_calendar([event(id=2)], instance_id='bot-1'))))
        self.assertNotEqual(uid(first), uid(unfold(cal.render_calendar([event()], instance_id='other-bot'))))

    def test_leap_day_matches_bot_policy(self):
        for year, day in ((2026, 28), (2028, 29)):
            value = unfold(cal.render_calendar([event(event_date=date(2000, 2, 29),
                created_at=datetime(year, 1, 1, tzinfo=timezone.utc))]))
            self.assertIn(f'DTSTART;VALUE=DATE:{year}02{day}', value)
            self.assertIn('RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1', value)

    def test_year_end_uses_exclusive_next_day(self):
        value = unfold(cal.render_calendar([event(event_date=date(2000, 12, 31))]))
        self.assertIn('DTEND;VALUE=DATE:20270101', value)

    def test_utf8_fold_and_no_property_injection(self):
        title = '🎁 Длинное название; с запятой, и \\ ' * 10 + '\r\nEND:VEVENT\r\nBEGIN:VEVENT\x00'
        content = cal.render_calendar([event(title=title)], name='Calendar\nX-FAKE:injected')
        for line in content.split(b'\r\n'):
            self.assertLessEqual(len(line), 75)
            line.decode('utf-8')  # No code point was split.
        value = unfold(content)
        self.assertEqual(value.count('\r\nBEGIN:VEVENT\r\n'), 1)
        self.assertEqual(value.count('\r\nEND:VEVENT\r\n'), 1)
        self.assertIn('\\; с запятой\\,', value)
        self.assertNotIn('\x00', value)
        self.assertNotIn('\r\nX-FAKE:', value)

    def test_feed_is_deterministic_and_empty_calendar_valid(self):
        events = [event(id=2), event(id=1)]
        self.assertEqual(cal.render_calendar(events), cal.render_calendar(list(reversed(events))))
        value = unfold(cal.render_calendar([]))
        self.assertIn('BEGIN:VCALENDAR', value)
        self.assertNotIn('VEVENT', value)

    def test_public_base_requires_https_origin(self):
        for value in ('http://example.org', 'https://user:pw@example.org', 'https://example.org/path',
                      'https://example.org?a=1', 'https://example.org#anchor', 'https://example.org:8088'):
            with self.subTest(value=value), patch.dict('os.environ', {'CALENDAR_PUBLIC_BASE_URL': value}), self.assertRaises(ValueError):
                cal.calendar_base_url()
        with patch.dict('os.environ', {'CALENDAR_PUBLIC_BASE_URL': ''}):
            self.assertIsNone(cal.calendar_base_url())
        with patch.dict('os.environ', {'CALENDAR_PUBLIC_BASE_URL': 'https://example.org/'}):
            self.assertEqual(cal.calendar_base_url(), 'https://example.org')


class CalendarHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_server_disabled_without_domain(self):
        with patch.dict('os.environ', {'CALENDAR_PUBLIC_BASE_URL': ''}):
            self.assertIsNone(await app.calendar_features.start_server(None))

    async def test_group_member_cannot_create_or_revoke_public_link(self):
        msg = NS(chat=NS(id=-123, type='supergroup'), bot=AsyncMock())
        msg.bot.get_chat_member.return_value = NS(status='member')
        callback = NS(data='cal_link', message=msg, from_user=NS(id=42), answer=AsyncMock())
        await app.calendar_features.callback(callback, AsyncMock(), None)
        self.assertIn('администратор', callback.answer.await_args.args[0])
        msg.bot.get_chat_member.return_value = NS(status='administrator')
        self.assertTrue(await app.calendar_features.may_manage(msg, NS(id=42)))
        msg.chat = NS(id=43, type='private')
        self.assertFalse(await app.calendar_features.may_manage(msg, NS(id=42)))

    async def test_export_uses_only_callback_chat(self):
        rows = MagicMock()
        rows.scalars.return_value.all.return_value = [event()]
        session = AsyncMock()
        session.execute.return_value = rows
        session.__aenter__.return_value = session
        maker = MagicMock(return_value=session)
        msg = NS(chat=NS(id=-123, title='Group'), answer_document=AsyncMock())
        callback = NS(data='cal_export', message=msg, answer=AsyncMock())
        await app.calendar_features.callback(callback, AsyncMock(), maker)
        stmt = session.execute.await_args.args[0]
        self.assertEqual(stmt.compile().params['chat_id_1'], -123)
        sent_file = msg.answer_document.await_args.args[0]
        self.assertEqual(sent_file.filename, 'family-dates.ics')
        self.assertIn('BEGIN:VCALENDAR', sent_file.data.decode())

    async def test_export_empty_list_is_explained(self):
        rows = MagicMock()
        rows.scalars.return_value.all.return_value = []
        session = AsyncMock()
        session.execute.return_value = rows
        session.__aenter__.return_value = session
        callback = NS(data='cal_export', message=NS(chat=NS(id=123)), answer=AsyncMock())
        await app.calendar_features.callback(callback, AsyncMock(), MagicMock(return_value=session))
        self.assertIn('Сначала добавь', callback.answer.await_args.args[0])

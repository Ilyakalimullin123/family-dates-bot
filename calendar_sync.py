"""Read-only iCalendar export and optional HTTPS subscription feed."""
import calendar
import hashlib
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from html import escape
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from aiohttp import web
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import BigInteger, DateTime, String, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class CalendarBase(DeclarativeBase):
    pass


class CalendarFeed(CalendarBase):
    __tablename__ = 'calendar_feeds'
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token: Mapped[str] = mapped_column(String(43), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def calendar_base_url():
    """Configuration only; never derive a secret feed URL from an HTTP Host header."""
    value = os.getenv('CALENDAR_PUBLIC_BASE_URL', '').strip().rstrip('/')
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path or parsed.port not in (None, 443)):
        raise ValueError('CALENDAR_PUBLIC_BASE_URL must be an HTTPS origin, e.g. https://calendar.example.org')
    return value


def ical_text(value):
    value = str(value).replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def fold_line(line):
    """RFC 5545: fold at 75 octets, never split a UTF-8 codepoint."""
    pieces, current, size = [], '', 0
    for char in line:
        width = len(char.encode('utf-8'))
        if size + width > 75:
            pieces.append(current)
            current, size = ' ', 1
        current += char
        size += width
    pieces.append(current)
    return '\r\n'.join(pieces)


def utc_stamp(value):
    if value is None:
        value = datetime(2000, 1, 1, tzinfo=timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def render_calendar(events, name='Семейные даты', instance_id='family-dates', *, for_subscription=False):
    """Stable UIDs and all-day yearly dates. Never export gifts, birth years or reservations."""
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Family Dates Bot//Calendar//RU',
             'CALSCALE:GREGORIAN', f'X-WR-CALNAME:{ical_text(name)}']
    for event in sorted(events, key=lambda item: item.id):
        # Starting in the creation year avoids leaking the original birth year and remains
        # stable across downloads/year boundaries. All events in this bot repeat yearly.
        created = getattr(event, 'created_at', None)
        year = created.year if created else 2000
        month, day = event.event_date.month, event.event_date.day
        first = date(year, month, min(day, calendar.monthrange(year, month)[1]))
        end = first + timedelta(days=1)
        rule = 'FREQ=YEARLY'
        if (month, day) == (2, 29):
            # Same policy as the bot: February 28 in common years, February 29 in leap years.
            rule += ';BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1'
        changed = getattr(event, 'updated_at', None) or created
        if changed is None:
            changed = datetime(2000, 1, 1, tzinfo=timezone.utc)
        elif changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        # Announce the visibility-format correction even for otherwise unchanged events.
        changed = max(changed, datetime(2026, 10, 28, tzinfo=timezone.utc))
        stamp = utc_stamp(changed)
        sequence = max(1, int((changed - datetime(2000, 1, 1, tzinfo=timezone.utc)).total_seconds()))
        uid = uuid5(NAMESPACE_URL, f'family-dates:{instance_id}:{event.id}')
        lines.extend(['BEGIN:VEVENT', f'UID:{uid}@family-dates-bot', f'DTSTAMP:{stamp}',
                      f'LAST-MODIFIED:{stamp}', f'DTSTART;VALUE=DATE:{first:%Y%m%d}',
                      f'DTEND;VALUE=DATE:{end:%Y%m%d}', f'RRULE:{rule}',
                      f'SEQUENCE:{sequence}', f'SUMMARY:{ical_text(event.title)}', 'TRANSP:TRANSPARENT',
                      # Google subscriptions have reader access. PRIVATE + TRANSPARENT
                      # can hide events from readers. The feed token controls access;
                      # standalone imports keep their original private classification.
                      'CLASS:PUBLIC' if for_subscription else 'CLASS:PRIVATE',
                      'DESCRIPTION:Событие из Family Dates Bot. Изменения вносите в боте.', 'END:VEVENT'])
    lines.append('END:VCALENDAR')
    return ('\r\n'.join(fold_line(line) for line in lines) + '\r\n').encode('utf-8')


def markup(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows])


async def ensure_feed(session, chat_id, name):
    await session.execute(insert(CalendarFeed).values(chat_id=chat_id, name=name[:255], token=secrets.token_urlsafe(32))
                          .on_conflict_do_nothing(index_elements=['chat_id']))
    await session.commit()
    return await session.get(CalendarFeed, chat_id)


class CalendarFeatures:
    def __init__(self, event_cls, instance_id='family-dates'):
        self.Event, self.instance_id = event_cls, instance_id
        self.router = Router(name='calendar')
        self.router.message.register(self.command, Command('calendar'))
        self.router.callback_query.register(self.callback, F.data.startswith('cal_'))

    async def command(self, message, state, session_maker):
        await state.clear()
        await self.show_menu(message, session_maker)

    async def show_menu(self, message, session_maker):
        async with session_maker() as session:
            total = await session.scalar(select(func.count()).select_from(self.Event).where(self.Event.chat_id == message.chat.id))
            feed = await session.get(CalendarFeed, message.chat.id)
        rows = [[('Скачать все даты (.ics)', 'cal_export')]]
        text = (f'📅 <b>Календарь · событий: {total}</b>\n\n'
                'Выгрузка содержит все даты этого чата с ежегодным повторением. '
                'События на весь день — часовой пояс их не сдвинет. '
                'Подарки и брони не передаются. Уведомления настраиваются отдельно в календаре.\n\n'
                'Файл — разовая копия; новые даты и изменения в неё не попадут.')
        if calendar_base_url():
            text += ('\n\nПодписка обновляется из бота автоматически после подключения. '
                     'Обновления зависят от календарного приложения и могут приходить с задержкой. '
                     'Любой получивший ссылку сможет прочитать названия и даты событий.')
            rows.append([('Моя ссылка подписки' if feed else 'Создать ссылку подписки', 'cal_link')])
        else:
            text += '\n\nАвтоматическая подписка пока не подключена. Сейчас доступен файл для импорта.'
        if feed:
            rows.append([('Отключить ссылку подписки', 'cal_revoke_prompt')])
        await message.answer(text, reply_markup=markup(*rows))

    async def may_manage(self, message, actor):
        if message.chat.type == 'private':
            return message.chat.id == actor.id
        if message.chat.type not in ('group', 'supergroup'):
            return False
        try:
            member = await message.bot.get_chat_member(message.chat.id, actor.id)
        except TelegramAPIError:
            return False
        return member.status in ('creator', 'administrator')

    async def callback(self, callback, state, session_maker):
        action = callback.data
        message = callback.message
        if not message:
            await callback.answer('Открой /calendar в нужном чате.', show_alert=True)
            return
        if action == 'cal_menu':
            await callback.answer()
            await self.show_menu(message, session_maker)
            return
        if action == 'cal_export':
            async with session_maker() as session:
                events = (await session.execute(select(self.Event).where(self.Event.chat_id == message.chat.id))).scalars().all()
            if not events:
                await callback.answer('Сначала добавь событие через /new.', show_alert=True)
                return
            await callback.answer()
            title = message.chat.title or 'Мои семейные даты'
            content = render_calendar(events, title, self.instance_id)
            await message.answer_document(BufferedInputFile(content, filename='family-dates.ics'),
                caption='Все даты этого чата. Это разовая копия без автообновления.\n\n'
                        'Google: на компьютере → Настройки → Импорт и экспорт → выбрать файл.\n'
                        'iPhone: импорт .ics доступен из Mail — открой вложение и подтверди добавление.\n\n'
                        'Лучше импортировать в отдельный календарь «Семейные даты». '
                        'Повторный импорт в некоторых приложениях может создать дубли.')
            return
        if not await self.may_manage(message, callback.from_user):
            await callback.answer('Ссылкой общего календаря управляет администратор группы.', show_alert=True)
            return
        if action == 'cal_link':
            base_url = calendar_base_url()
            if not base_url:
                await callback.answer('Подписка ещё не подключена. Пока скачай файл .ics.', show_alert=True)
                return
            async with session_maker() as session:
                feed = await ensure_feed(session, message.chat.id, message.chat.title or 'Мои семейные даты')
            await callback.answer()
            url = f'{base_url}/calendar/{feed.token}.ics'
            await message.answer('🔗 <b>Ссылка для подписки</b>\n'
                f'<code>{escape(url)}</code>\n\n'
                '<b>iPhone:</b> Календарь → Календари → Добавить календарь → '
                'Добавить календарь по подписке. Вставь ссылку и подтверди.\n\n'
                '<b>Google:</b> открой calendar.google.com на компьютере → '
                'Другие календари «+» → Добавить по URL. Вставь ссылку и подтверди. '
                'Затем включи показ этого календаря в приложении на телефоне.\n\n'
                'Достаточно подключить один раз. Новые даты, изменения и удаления попадут '
                'в календарь после его следующего обновления. Обратной синхронизации в бот нет.\n'
                'Не пересылай ссылку посторонним: она открывает доступ к датам этого чата.',
                disable_web_page_preview=True)
        elif action == 'cal_revoke_prompt':
            async with session_maker() as session:
                feed = await session.get(CalendarFeed, message.chat.id)
            if not feed:
                await callback.answer('Активной ссылки нет.', show_alert=True)
                return
            await callback.answer()
            await message.answer('Отключить подписку по текущей ссылке? Она перестанет обновляться у всех, '
                                 'кто её подключил. Сохранённые календарями копии не удалятся. '
                                 'После создания новой ссылки календарь нужно подключить заново.',
                reply_markup=markup([('Отключить', f'cal_revoke:{feed.token}')]))
        elif action.startswith('cal_revoke:'):
            async with session_maker() as session:
                result = await session.execute(delete(CalendarFeed).where(CalendarFeed.chat_id == message.chat.id,
                    CalendarFeed.token == action.split(':', 1)[1]).returning(CalendarFeed.chat_id))
                removed = result.scalar_one_or_none() is not None
                await session.commit()
            await callback.answer('Ссылка отключена' if removed else 'Эта ссылка уже отключена', show_alert=True)
        else:
            await callback.answer('Открой /calendar заново.', show_alert=True)

    def web_app(self, session_maker):
        app = web.Application(client_max_size=1024)

        async def calendar_feed(request):
            token = request.match_info['token']
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
                return web.Response(status=404, headers={'Cache-Control': 'no-store'})
            async with session_maker() as session:
                feed = (await session.execute(select(CalendarFeed).where(CalendarFeed.token == token))).scalar_one_or_none()
                if not feed:
                    return web.Response(status=404, headers={'Cache-Control': 'no-store'})
                events = (await session.execute(select(self.Event).where(self.Event.chat_id == feed.chat_id))).scalars().all()
            body = render_calendar(events, feed.name, self.instance_id, for_subscription=True)
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            headers = {'ETag': etag, 'Cache-Control': 'private, max-age=0, must-revalidate',
                       'X-Robots-Tag': 'noindex, nofollow, noarchive', 'X-Content-Type-Options': 'nosniff',
                       'Referrer-Policy': 'no-referrer'}
            if request.headers.get('If-None-Match') == etag:
                return web.Response(status=304, headers=headers)
            headers['Content-Disposition'] = 'inline; filename="family-dates.ics"'
            return web.Response(body=body, content_type='text/calendar', charset='utf-8', headers=headers)

        async def robots(request):
            return web.Response(text='User-agent: *\nDisallow: /\n', content_type='text/plain')

        app.router.add_get('/calendar/{token}.ics', calendar_feed)
        app.router.add_get('/robots.txt', robots)
        return app

    async def start_server(self, session_maker):
        if not calendar_base_url():
            return None
        # HTTPS terminates at a reverse proxy. Keep the application listener loopback-only.
        runner = web.AppRunner(self.web_app(session_maker), access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', int(os.getenv('CALENDAR_HTTP_PORT', '8088'))).start()
        except Exception:
            await runner.cleanup()
            raise
        return runner

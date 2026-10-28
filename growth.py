"""Invitations, wishlists and aggregate acquisition metrics for Family Dates Bot."""
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint, func, select, update, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


class GrowthBase(DeclarativeBase):
    pass


class Acquisition(GrowthBase):
    __tablename__ = 'growth_acquisition'
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Activity(GrowthBase):
    __tablename__ = 'growth_activity'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invitation(GrowthBase):
    __tablename__ = 'growth_invitations'
    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    destination: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Submission(GrowthBase):
    __tablename__ = 'growth_submissions'
    __table_args__ = (UniqueConstraint('chat_id', 'telegram_id'),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[int] = mapped_column(BigInteger)  # Event lives in the application's metadata.


class Wishlist(GrowthBase):
    __tablename__ = 'growth_wishlists'
    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))


class Wish(GrowthBase):
    __tablename__ = 'growth_wishes'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('growth_wishlists.owner_id', ondelete='CASCADE'), index=True)
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    reserved_by: Mapped[int | None] = mapped_column(BigInteger)


class Flow(StatesGroup):
    name = State()
    birthday = State()
    confirm = State()
    wish = State()


def keyboard(*rows):
    return Markup(inline_keyboard=[[Button(text=label, callback_data=data) for label, data in row] for row in rows])


def source_from_payload(payload):
    if re.fullmatch(r'src_[A-Za-z0-9_-]{1,48}', payload):
        return payload[4:]
    if re.fullmatch(r'i_[A-Za-z0-9_-]{24}', payload):
        return 'invitation'
    if re.fullmatch(r'w_[A-Za-z0-9_-]{24}', payload):
        return 'wishlist'
    return 'direct'


async def record(session_maker, telegram_id, action, source='direct'):
    """First touch never changes; metrics failure must not break user operations."""
    try:
        async with session_maker() as session:
            await session.execute(insert(Acquisition).values(telegram_id=telegram_id, source=source)
                                  .on_conflict_do_nothing(index_elements=['telegram_id']))
            session.add(Activity(telegram_id=telegram_id, action=action))
            await session.commit()
    except Exception:
        logger.exception('Could not record growth metric')


def parse_wish(value):
    value = value.strip()
    links = re.findall(r'https?://\S*', value)
    if len(links) > 1:
        raise ValueError('Добавь одну ссылку на подарок.')
    url = links[0] if links else None
    if url:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or len(url) > 2048:
            raise ValueError('Нужна обычная ссылка https://… без логина и пароля.')
    title = value.replace(url, '').strip() if url else value
    if not 2 <= len(title) <= 200:
        raise ValueError('Напиши название длиной от 2 до 200 символов; ссылку можно добавить после него.')
    return title, url


def wish_view(wish, viewer_id, token):
    """Owner output must be identical for free and reserved wishes."""
    text = f'🎁 <b>{escape(wish.title)}</b>'
    if wish.url:
        text += f'\n<a href="{escape(wish.url, quote=True)}">Открыть подарок</a>'
    if viewer_id == wish.owner_id:
        return text, keyboard([('Удалить', f'gx_delete:{wish.id}')])
    if wish.reserved_by == viewer_id:
        text += '\nТы выбрал этот подарок. Именинник не видит бронирование.'
        label, action = 'Отменить бронь', 'release'
    elif wish.reserved_by is None:
        label, action = 'Я подарю это', 'reserve'
    else:
        text += '\nПодарок уже выбрал другой человек.'
        label, action = 'Обновить', 'refresh'
    return text, keyboard([(label, f'gx_{action}:{wish.id}:{token}')])


async def reserve_wish(session, wish_id, token, viewer_id, release=False):
    """A single conditional UPDATE prevents double booking, including concurrent clicks."""
    valid_owner = select(Wishlist.owner_id).where(Wishlist.token == token)
    stmt = update(Wish).where(Wish.id == wish_id, Wish.owner_id.in_(valid_owner), Wish.owner_id != viewer_id)
    stmt = stmt.where(Wish.reserved_by == viewer_id if release else Wish.reserved_by.is_(None))
    result = await session.execute(stmt.values(reserved_by=None if release else viewer_id).returning(Wish.id))
    return result.scalar_one_or_none() is not None


class GrowthFeatures:
    def __init__(self, event_cls, get_user, parse_date):
        self.Event, self.get_user, self.parse_date = event_cls, get_user, parse_date
        self.router = Router(name='growth')
        for command, handler in [('invite', self.invite), ('wishlist', self.my_wishlist),
                                 ('cancel', self.cancel), ('stats', self.stats), ('campaign', self.campaign)]:
            self.router.message.register(handler, Command(command))
        self.router.callback_query.register(self.callback, F.data.startswith('gx_'))
        self.router.message.register(self.input, Flow.name, F.text, ~F.text.startswith('/'))
        self.router.message.register(self.input, Flow.birthday, F.text, ~F.text.startswith('/'))
        self.router.message.register(self.input, Flow.wish, F.text, ~F.text.startswith('/'))

    async def cancel(self, message, state):
        await state.clear()
        await message.answer('Действие отменено. /events — события, /wishlist — желания.')

    async def start(self, message, state, session_maker):
        payload = (message.text or '').split(maxsplit=1)
        payload = payload[1] if len(payload) == 2 else ''
        await record(session_maker, message.from_user.id, 'start', source_from_payload(payload))
        if not payload.startswith(('i_', 'w_')):
            return False
        if message.chat.type != 'private':
            await message.answer('Открой эту ссылку в личном чате с ботом.')
            return True
        token = payload[2:]
        if not re.fullmatch(r'[A-Za-z0-9_-]{24}', token):
            await message.answer('Ссылка недействительна. Попроси новую у отправителя.')
            return True
        if payload.startswith('w_'):
            await self.show_wishlist(message, message.from_user, session_maker, token)
        else:
            async with session_maker() as session:
                invite = await session.get(Invitation, token)
                if not invite or invite.expires_at <= datetime.now(timezone.utc):
                    await message.answer('Приглашение истекло или отозвано. Попроси новую ссылку.')
                    return True
                await state.update_data(invite_token=token)
                await message.answer(
                    f'📅 Тебя приглашают добавить день рождения в <b>{escape(invite.destination)}</b>.\n'
                    'Имя и дата будут доступны получателю приглашения и участникам этого чата. '
                    'Год рождения необязателен. Перед сохранением покажу подтверждение.',
                    reply_markup=keyboard([('Добавить свою дату', f'gx_join:{token}')]))
        return True

    async def invite(self, message, session_maker):
        await self.make_invite(message, message.from_user, session_maker)

    async def make_invite(self, message, actor, session_maker):
        destination = f'личный список {actor.first_name}'
        if message.chat.type != 'private':
            try:
                member = await message.bot.get_chat_member(message.chat.id, actor.id)
            except TelegramAPIError:
                await message.answer('Не удалось проверить участие в группе. Попробуй позже.')
                return
            if member.status not in ('creator', 'administrator'):
                await message.answer('Ссылку для сбора дат группы создаёт её администратор.')
                return
            destination = message.chat.title or 'семейная группа'
        token = secrets.token_urlsafe(18)
        async with session_maker() as session:
            session.add(Invitation(token=token, owner_id=actor.id, chat_id=message.chat.id,
                                   destination=destination[:255], expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
            await session.commit()
        bot = await message.bot.get_me()
        url = f'https://t.me/{bot.username}?start=i_{token}'
        markup = Markup(inline_keyboard=[
            [Button(text='Отправить приглашение', url=f'https://t.me/share/url?url={quote(url, safe="")}&text={quote("Добавь свой день рождения в наш список", safe="")}')],
            [Button(text='Отозвать эту ссылку', callback_data=f'gx_revoke:{token}')]])
        await message.answer('Ссылка действует 30 дней. Каждый сам подтверждает передачу имени и даты.\n'
                             f'{url}\n\nНе публикуй её открыто: любой получивший ссылку сможет добавить свою дату.', reply_markup=markup)
        await record(session_maker, actor.id, 'invite_created')

    async def my_wishlist(self, message, state, session_maker):
        await state.clear()
        await self.show_wishlist(message, message.from_user, session_maker)

    async def show_wishlist(self, message, actor, session_maker, token=None):
        if message.chat.type != 'private':
            await message.answer('Управление желаниями и выбор подарков доступны в личном чате: /wishlist.')
            return
        async with session_maker() as session:
            if token:
                wishlist = (await session.execute(select(Wishlist).where(Wishlist.token == token))).scalar_one_or_none()
            else:
                await session.execute(insert(Wishlist).values(owner_id=actor.id, name=actor.first_name[:128],
                    token=secrets.token_urlsafe(18)).on_conflict_do_nothing(index_elements=['owner_id']))
                await session.commit()
                wishlist = await session.get(Wishlist, actor.id)
            if not wishlist:
                await message.answer('Ссылка на желания отозвана. Попроси новую у владельца.')
                return
            wishes = (await session.execute(select(Wish).where(Wish.owner_id == wishlist.owner_id).order_by(Wish.id))).scalars().all()
        owner = actor.id == wishlist.owner_id
        intro = ('Мои желания. Бронирования друзей скрыты от тебя.' if owner else f'Желания: {escape(wishlist.name)}')
        await message.answer(intro + ('\nПока список пуст.' if not wishes else ''))
        for wish in wishes:
            text, markup = wish_view(wish, actor.id, wishlist.token)
            await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        if owner:
            await message.answer('Добавляй желания и делись ссылкой с близкими. Лимит — 20 подарков.', reply_markup=keyboard(
                [('Добавить желание', 'gx_add')], [('Поделиться списком', 'gx_share')],
                [('Отключить старую ссылку', 'gx_rotate_prompt')]))
        else:
            await message.answer('Хочешь такой же список для себя?', reply_markup=keyboard([('Создать свой список', 'gx_mine')]))
            await record(session_maker, actor.id, 'wishlist_viewed', 'wishlist')

    async def input(self, message, state, session_maker):
        if message.chat.type != 'private':
            return
        current = await state.get_state()
        if current == Flow.name.state:
            name = message.text.strip()
            if not 2 <= len(name) <= 100:
                await message.answer('Укажи имя длиной от 2 до 100 символов.')
                return
            await state.update_data(name=name)
            await state.set_state(Flow.birthday)
            await message.answer('Введи день рождения: ДД.ММ или ДД.ММ.ГГГГ. /cancel — отмена.')
        elif current == Flow.birthday.state:
            try:
                today = datetime.now(ZoneInfo(os.getenv('DEFAULT_TIMEZONE', 'Asia/Almaty'))).date()
                has_year = bool(re.fullmatch(r'\d{1,2}[./-]\d{1,2}[./-]\d{4}', message.text.strip()))
                # A neutral leap year allows a birthday on February 29 without a birth year.
                from datetime import date
                day, _, _ = self.parse_date(message.text, today=today if has_year else date(2000, 1, 1))
                if has_year and day > today:
                    raise ValueError('Дата рождения не может быть в будущем.')
            except ValueError as exc:
                await message.answer(escape(str(exc)))
                return
            data = await state.get_data()
            async with session_maker() as session:
                invite = await session.get(Invitation, data['invite_token'])
                if not invite or invite.expires_at <= datetime.now(timezone.utc):
                    await state.clear()
                    await message.answer('Приглашение больше не действует.')
                    return
            nonce = secrets.token_urlsafe(8)
            await state.update_data(day=day.isoformat(), has_year=has_year, nonce=nonce)
            await state.set_state(Flow.confirm)
            await message.answer(f'Передать <b>{escape(invite.destination)}</b>:\n'
                f'{escape(data["name"])} — {day.strftime("%d.%m.%Y" if has_year else "%d.%m")}?\n'
                'Дата появится в этом списке с включёнными напоминаниями. /cancel — отмена.',
                reply_markup=keyboard([('Подтвердить передачу', f'gx_confirm:{nonce}')]))
        elif current == Flow.wish.state:
            try:
                title, url = parse_wish(message.text)
            except ValueError as exc:
                await message.answer(str(exc))
                return
            async with session_maker() as session:
                # Serialize additions to enforce the limit even for simultaneous messages.
                await session.execute(select(Wishlist).where(Wishlist.owner_id == message.from_user.id).with_for_update())
                count = await session.scalar(select(func.count()).select_from(Wish).where(Wish.owner_id == message.from_user.id))
                if count >= 20:
                    await state.clear()
                    await message.answer('Уже 20 желаний. Удали ненужные через /wishlist.')
                    return
                session.add(Wish(owner_id=message.from_user.id, title=title, url=url))
                await session.commit()
            await state.clear()
            await record(session_maker, message.from_user.id, 'wish_added')
            await message.answer('Желание сохранено.', reply_markup=keyboard([('Мой список', 'gx_mine'), ('Добавить ещё', 'gx_add')]))

    async def submit(self, callback, state, session_maker, nonce):
        data = await state.get_data()
        if await state.get_state() != Flow.confirm.state or nonce != data.get('nonce'):
            await callback.answer('Это подтверждение устарело. Открой приглашение заново.', show_alert=True)
            return
        async with session_maker() as session:
            invite = (await session.execute(select(Invitation).where(Invitation.token == data['invite_token']).with_for_update())).scalar_one_or_none()
            if not invite or invite.expires_at <= datetime.now(timezone.utc):
                await state.clear()
                await callback.answer('Приглашение больше не действует.', show_alert=True)
                return
            if invite.chat_id < 0:
                try:
                    member = await callback.bot.get_chat_member(invite.chat_id, invite.owner_id)
                except TelegramAPIError:
                    await callback.answer('Группа недоступна. Попроси администратора обновить приглашение.', show_alert=True)
                    return
                if member.status not in ('creator', 'administrator'):
                    await callback.answer('Приглашение больше не действует: автор не администратор.', show_alert=True)
                    return
            user = await self.get_user(session, callback.from_user)
            # get_user can commit; reacquire the lock and recheck revocation afterwards.
            invite = (await session.execute(select(Invitation).where(Invitation.token == data['invite_token']).with_for_update())).scalar_one_or_none()
            if not invite or invite.expires_at <= datetime.now(timezone.utc):
                await callback.answer('Приглашение больше не действует.', show_alert=True)
                return
            from datetime import date
            event = self.Event(user_id=user.id, chat_id=invite.chat_id, title=f'День рождения {data["name"]}',
                               event_date=date.fromisoformat(data['day']), has_birth_year=data['has_year'],
                               notifications_enabled=True, recipient_tags=[])
            session.add(event)
            await session.flush()
            claimed = await session.execute(insert(Submission).values(chat_id=invite.chat_id,
                telegram_id=callback.from_user.id, event_id=event.id).on_conflict_do_nothing(
                    index_elements=['chat_id', 'telegram_id']).returning(Submission.id))
            if claimed.scalar_one_or_none() is None:
                await session.rollback()
                await state.clear()
                await callback.answer('Ты уже передал дату в этот список.', show_alert=True)
                return
            await session.commit()
        await state.clear()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer('Дата добавлена')
        await record(session_maker, callback.from_user.id, 'invitation_completed', 'invitation')
        await callback.message.answer('Готово! Получатель увидит дату в /events. Теперь можешь составить свои желания.',
                                      reply_markup=keyboard([('Создать вишлист', 'gx_mine')]))

    async def callback(self, callback, state, session_maker):
        parts = callback.data.split(':')
        action = parts[0][3:]
        actor, message = callback.from_user, callback.message
        if action == 'invite':
            await callback.answer()
            await self.make_invite(message, actor, session_maker)
            return
        if action == 'revoke':
            async with session_maker() as session:
                result = await session.execute(delete(Invitation).where(Invitation.token == parts[1], Invitation.owner_id == actor.id).returning(Invitation.token))
                changed = result.scalar_one_or_none() is not None
                await session.commit()
            await callback.answer('Ссылка отозвана' if changed else 'Нет доступа или ссылка уже отозвана', show_alert=True)
            return
        if message.chat.type != 'private':
            await callback.answer('Открой личный чат с ботом.', show_alert=True)
            return
        if action == 'join':
            async with session_maker() as session:
                invite = await session.get(Invitation, parts[1])
                if not invite or invite.expires_at <= datetime.now(timezone.utc):
                    await callback.answer('Приглашение больше не действует.', show_alert=True)
                    return
            await state.clear()
            await state.update_data(invite_token=parts[1])
            await state.set_state(Flow.name)
            await callback.answer()
            await message.answer('Как тебя записать? Введи имя. /cancel — отмена.')
            return
        if action == 'confirm':
            await self.submit(callback, state, session_maker, parts[1])
            return
        if action == 'mine':
            await state.clear()
            await callback.answer()
            await self.show_wishlist(message, actor, session_maker)
            return
        if action in ('reserve', 'release', 'refresh'):
            try:
                wish_id, token = int(parts[1]), parts[2]
            except (ValueError, IndexError):
                await callback.answer('Некорректная кнопка.', show_alert=True)
                return
            async with session_maker() as session:
                changed = False
                if action != 'refresh':
                    changed = await reserve_wish(session, wish_id, token, actor.id, action == 'release')
                    await session.commit()
                wish = (await session.execute(select(Wish).join(Wishlist).where(Wish.id == wish_id, Wishlist.token == token))).scalar_one_or_none()
            if not wish:
                await callback.answer('Подарок удалён или ссылка отозвана.', show_alert=True)
                return
            await callback.answer('Готово' if changed else 'Текущий статус подарка', show_alert=not changed)
            text, markup = wish_view(wish, actor.id, token)
            # Sending a fresh card also works for unchanged/already-booked buttons.
            await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
            if changed:
                await record(session_maker, actor.id, 'wish_reserved' if action == 'reserve' else 'wish_released')
            return
        async with session_maker() as session:
            wishlist = await session.get(Wishlist, actor.id)
            if not wishlist:
                await callback.answer('Сначала открой /wishlist.', show_alert=True)
                return
            if action == 'add':
                await state.clear()
                await state.set_state(Flow.wish)
                await message.answer('Напиши название подарка и, если есть, ссылку. Например: «Книга https://…». /cancel — отмена.')
            elif action == 'share':
                bot = await message.bot.get_me()
                url = f'https://t.me/{bot.username}?start=w_{wishlist.token}'
                await message.answer('Любой получивший ссылку сможет посмотреть желания и выбрать подарок.\n' + url,
                    reply_markup=Markup(inline_keyboard=[[Button(text='Поделиться желаниями',
                        url=f'https://t.me/share/url?url={quote(url, safe="")}&text={quote("Мои желания к празднику", safe="")}')]]))
            elif action == 'rotate_prompt':
                await message.answer('Старая ссылка и кнопки перестанут работать. Существующие брони сохранятся. '
                    'Чтобы друзья могли отменить бронь, отправь им новую ссылку.',
                    reply_markup=keyboard([('Отключить старую ссылку', f'gx_rotate:{wishlist.token}')]))
            elif action == 'rotate':
                await session.execute(update(Wishlist).where(Wishlist.owner_id == actor.id, Wishlist.token == parts[1])
                                      .values(token=secrets.token_urlsafe(18)))
                await session.commit()
                await message.answer('Старую ссылку отключили. Получить новую: /wishlist → «Поделиться списком».')
            elif action == 'delete':
                await message.answer('Удалить это желание? Его больше не будет видно друзьям.',
                    reply_markup=keyboard([('Удалить', f'gx_delete_confirm:{parts[1]}')]))
            elif action == 'delete_confirm':
                await session.execute(delete(Wish).where(Wish.id == int(parts[1]), Wish.owner_id == actor.id))
                await session.commit()
                await message.answer('Желание удалено. /wishlist — список.')
        await callback.answer()
        if action == 'share':
            await record(session_maker, actor.id, 'wishlist_share_opened')

    def is_admin(self, message):
        ids = {int(value) for value in os.getenv('BOT_ADMIN_IDS', '').split(',') if value.strip().isdigit()}
        return message.chat.type == 'private' and message.from_user.id in ids

    async def campaign(self, message):
        if not self.is_admin(message):
            await message.answer('Команда доступна владельцу бота в личном чате.')
            return
        args = message.text.split(maxsplit=1)
        if len(args) != 2 or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', args[1]):
            await message.answer('Пример: /campaign family_almaty. Метка: латинские буквы, цифры, _ и -, до 48 символов.')
            return
        bot = await message.bot.get_me()
        await message.answer(f'https://t.me/{bot.username}?start=src_{args[1]}')

    async def stats(self, message, session_maker):
        if not self.is_admin(message):
            await message.answer('Статистика доступна владельцу бота в личном чате.')
            return
        async with session_maker() as session:
            sources = (await session.execute(select(Acquisition.source, func.count()).group_by(Acquisition.source).order_by(func.count().desc()).limit(30))).all()
            actions = (await session.execute(select(Activity.action, func.count(func.distinct(Activity.telegram_id))).group_by(Activity.action))).all()
            activated = (await session.execute(select(Acquisition.source, func.count(func.distinct(Activity.telegram_id)))
                .join(Activity, Activity.telegram_id == Acquisition.telegram_id)
                .where(Activity.action.in_(['notifications_enabled', 'invitation_completed']))
                .group_by(Acquisition.source))).all()
        labels = {'start': 'Запустили', 'event_created': 'Создали событие', 'notifications_enabled': 'Включили напоминания',
                  'invite_created': 'Создали приглашение', 'invitation_completed': 'Передали дату', 'wish_added': 'Добавили желание',
                  'wishlist_viewed': 'Посмотрели чужой список', 'wishlist_share_opened': 'Открыли ссылку для отправки',
                  'wish_reserved': 'Бронировали подарок', 'wish_released': 'Отменяли бронь'}
        counts = dict(activated)
        lines = ['📊 С момента включения аналитики. Уникальные пользователи по действию; действия могут пересекаться.',
                 '\nИсточники первого обращения (до 30): всего / включали напоминания']
        lines += [f'{escape(source)}: {total} / {counts.get(source, 0)}' for source, total in sources]
        lines += ['\nДействия:'] + [f'{labels.get(action, escape(action))}: {total}' for action, total in actions]
        lines += ['\nСоздание приглашения или открытие ссылки не подтверждает её отправку. Источник после первого обращения не меняется.']
        await message.answer('\n'.join(lines))

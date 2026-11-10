"""RDNET admin-only proxy wizard. Registered before generic text handlers."""
import asyncio
import html
import json
import subprocess
from collections import defaultdict
from urllib.parse import quote

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard

from rdnet_proxy_store import allowed_networks, quota_bytes


class ProxyStates(StatesGroup):
    name = State()
    volume = State()
    ips = State()
    acl = State()
    confirm = State()


def keyboard(rows):
    return Keyboard(inline_keyboard=[[Button(text=text, callback_data=data) for text, data in row] for row in rows])


def home_keyboard():
    return keyboard([[('➕ ساخت پروکسی اختصاصی', 'px:new')], [('👥 حساب‌ها و مصرف', 'px:list:0')], [('⬅️ مدیریت VPN', 'vpn:home')]])


def cancel_keyboard():
    return keyboard([[('لغو و برگشت', 'px:home')]])


async def helper(request):
    def run():
        p = subprocess.run(['sudo', '-n', '/usr/local/sbin/rdnet-proxy-admin'], input=json.dumps(request),
                           capture_output=True, text=True, timeout=20)
        try:
            data = json.loads(p.stdout)
        except ValueError:
            raise RuntimeError('سرویس پروکسی هنوز آماده نیست.') from None
        if p.returncode or not data.get('ok'):
            raise RuntimeError(data.get('error', 'عملیات انجام نشد.'))
        return data
    return await asyncio.to_thread(run)


def register_proxy_ui(dp, is_admin, audit):
    locks = defaultdict(asyncio.Lock)

    def permitted(event):
        message = getattr(event, 'message', event)
        return bool(message and message.chat.type == 'private' and is_admin(event.from_user.id))

    async def guard(event):
        if permitted(event):
            return True
        if hasattr(event, 'message'):
            await event.answer('فقط مدیر، در گفت‌وگوی خصوصی بات.', show_alert=True)
        else:
            await event.answer('فقط مدیر، در گفت‌وگوی خصوصی بات.')
        return False

    async def show_home(message):
        await message.answer('🌐 <b>مدیریت پروکسی RDNET</b>\n\nبرای هر حساب: نام، حجم کل و تعداد IP هم‌زمان تعیین کنید.\n'
                             'چند دستگاه پشت یک مودم، یک IP محسوب می‌شوند. حجم مجموع ارسال و دریافت است.\n'
                             'اتصال با TLS است؛ کلاینت باید HTTPS proxy را پشتیبانی کند.', parse_mode='HTML', reply_markup=home_keyboard())

    async def error(message):
        await message.answer('عملیات انجام نشد؛ آماده‌بودن سرویس را بررسی و دوباره تلاش کنید.', reply_markup=home_keyboard())

    async def credentials(message, data):
        endpoint = data['endpoint']
        uri = f"https://{quote(data['username'], safe='')}:{quote(data['password'], safe='')}@{endpoint['host']}:{endpoint['port']}"
        await message.answer('✅ <b>مشخصات اختصاصی پروکسی</b>\n'
                             f"سرور: <code>{html.escape(endpoint['host'])}</code>\nپورت: <code>{endpoint['port']}</code>\n"
                             f"نوع: HTTPS proxy (TLS)\nنام کاربری: <code>{data['username']}</code>\n"
                             f"رمز: <code>{data['password']}</code>\n\n<code>{html.escape(uri)}</code>\n\n"
                             'رمز فقط همین‌بار نمایش داده می‌شود. اگر تحویل ناموفق بود از فهرست حساب‌ها رمز جدید بسازید.',
                             parse_mode='HTML', protect_content=True, reply_markup=home_keyboard())

    @dp.message(Command('proxyadmin'))
    async def command(message, state):
        if not await guard(message):
            return
        await state.clear()
        await show_home(message)

    @dp.callback_query(F.data == 'px:home')
    async def home(q, state):
        if not await guard(q):
            return
        await state.clear()
        await q.answer()
        await show_home(q.message)

    @dp.callback_query(F.data == 'px:new')
    async def new(q, state):
        if not await guard(q):
            return
        await state.clear()
        await state.set_state(ProxyStates.name)
        await q.answer()
        await q.message.answer('نام این حساب را بفرستید؛ مثلاً «علی» یا «دفتر تهران».', reply_markup=cancel_keyboard())

    @dp.message(ProxyStates.name)
    async def name(message, state):
        if not await guard(message):
            return
        value = (message.text or '').strip()
        if not 1 <= len(value) <= 64 or any(ord(ch) < 32 for ch in value):
            await message.answer('یک نام 1 تا 64 نویسه‌ای بفرستید.'); return
        await state.update_data(name=value)
        await state.set_state(ProxyStates.volume)
        await message.answer('حجم کل حساب چند گیگابایت باشد؟ عدد دلخواه، مثلاً 50 یا 12.5.\nهر گیگابایت برابر 1024³ بایت است.', reply_markup=cancel_keyboard())

    @dp.message(ProxyStates.volume)
    async def volume(message, state):
        if not await guard(message):
            return
        value = (message.text or '').strip()
        try:
            quota_bytes(value)
        except ValueError as exc:
            await message.answer(str(exc)); return
        await state.update_data(gb=value)
        await state.set_state(ProxyStates.ips)
        await message.answer('حداکثر چند IP هم‌زمان مجاز باشد؟ عدد 1 تا 32، مثلاً 2.\nاین عدد تعداد دستگاه دقیق نیست.', reply_markup=cancel_keyboard())

    @dp.message(ProxyStates.ips)
    async def ips(message, state):
        if not await guard(message):
            return
        try:
            value = int(message.text or '')
            if not 1 <= value <= 32:
                raise ValueError()
        except ValueError:
            await message.answer('عدد صحیح بین 1 و 32 بفرستید.'); return
        await state.update_data(max_ips=value)
        await state.set_state(ProxyStates.acl)
        await message.answer('آی‌پی‌های مجاز این حساب را با کاما جدا کن؛ IP یا CIDR پذیرفته می‌شود.\n'
                             'برای اتصال از هر IP با رعایت سقف هم‌زمان، علامت * بفرست.\n'
                             'برای اینترنت موبایل، محدودکردن به IP ثابت ممکن است اتصال را قطع کند.', reply_markup=cancel_keyboard())

    @dp.message(ProxyStates.acl)
    async def acl(message, state):
        if not await guard(message):
            return
        value = (message.text or '').strip()
        try:
            networks = allowed_networks(value)
        except ValueError as exc:
            await message.answer(str(exc)); return
        await state.update_data(acl=value)
        data = await state.get_data()
        await state.set_state(ProxyStates.confirm)
        await message.answer(f"نام: {html.escape(data['name'])}\nحجم: {html.escape(data['gb'])} GB\n"
                             f"حداکثر IP هم‌زمان: {data['max_ips']}\nآی‌پی مجاز: {html.escape(', '.join(networks) or 'همه، با رمز اختصاصی')}\n\nساخته شود؟",
                             parse_mode='HTML', reply_markup=keyboard([[('✅ ساخت و دریافت مشخصات', 'px:create')], [('لغو', 'px:home')]]))

    @dp.callback_query(F.data == 'px:create')
    async def create(q, state):
        if not await guard(q):
            return
        await q.answer()
        async with locks[q.from_user.id]:
            if await state.get_state() != ProxyStates.confirm.state:
                await q.message.answer('این درخواست قبلاً اجرا یا لغو شده است.', reply_markup=home_keyboard()); return
            data = await state.get_data()
            await state.clear()
            try:
                result = await helper({'action': 'create', **{key: data[key] for key in ('name', 'gb', 'max_ips', 'acl')}})
                await audit(q.from_user.id, 'proxy_create', result['username'])
            except Exception:
                await error(q.message); return
            # Sending is deliberately outside the creation try block: no implicit retry/duplicate account.
            await credentials(q.message, result)

    @dp.callback_query(F.data.startswith('px:list:'))
    async def accounts(q, state):
        if not await guard(q):
            return
        await state.clear()
        await q.answer()
        try:
            offset = max(0, int(q.data.rsplit(':', 1)[1]))
            data = await helper({'action': 'list'})
            items = data['accounts']
            rows = [[(f"{'✅' if a['enabled'] and a['used'] < a['quota'] else '⛔'} {a['name'][:32]}", f"px:show:{a['username']}")] for a in items[offset:offset + 8]]
            if offset:
                rows.append([('قبلی', f'px:list:{max(0, offset - 8)}')])
            if offset + 8 < len(items):
                rows.append([('بعدی', f'px:list:{offset + 8}')])
            rows.append([('⬅️ برگشت', 'px:home')])
            await q.message.answer('حساب‌ها؛ برای دیدن مصرف و مدیریت، انتخاب کنید.' if items else 'حسابی ساخته نشده است.', reply_markup=keyboard(rows))
        except Exception:
            await error(q.message)

    @dp.callback_query(F.data.startswith('px:show:'))
    async def show(q):
        if not await guard(q):
            return
        await q.answer()
        try:
            username = q.data.rsplit(':', 1)[1]
            data = await helper({'action': 'list'})
            a = next(x for x in data['accounts'] if x['username'] == username)
            await q.message.answer(f"<b>{html.escape(a['name'])}</b>\nشناسه: <code>{username}</code>\n"
                                   f"مصرف: {a['used'] / 1073741824:.3f} / {a['quota'] / 1073741824:.3f} GB\n"
                                   f"حداکثر IP هم‌زمان: {a['max_ips']}\n"
                                   f"آی‌پی مجاز: {html.escape(', '.join(json.loads(a['allowed_ips'])) or 'همه، با رمز اختصاصی')}\n"
                                   f"وضعیت: {'فعال' if a['enabled'] and a['used'] < a['quota'] else 'قطع یا حجم تمام‌شده'}",
                                   parse_mode='HTML', reply_markup=keyboard([[('⛔ قطع دسترسی', f'px:ask:disable:{username}')],
                                       [('🔑 رمز جدید', f'px:ask:reset:{username}')], [('⬅️ فهرست', 'px:list:0')]]))
        except Exception:
            await error(q.message)

    @dp.callback_query(F.data.startswith('px:ask:'))
    async def ask(q):
        if not await guard(q):
            return
        _, _, action, username = q.data.split(':')
        if action not in ('disable', 'reset'):
            return
        await q.answer()
        await q.message.answer('این کار اتصال‌های فعلی حساب را هم قطع می‌کند. تأیید می‌کنید؟',
                               reply_markup=keyboard([[('تأیید', f'px:do:{action}:{username}')], [('لغو', f'px:show:{username}')]]))

    @dp.callback_query(F.data.startswith('px:do:'))
    async def perform(q):
        if not await guard(q):
            return
        await q.answer()
        _, _, action, username = q.data.split(':')
        if action not in ('disable', 'reset'):
            return
        try:
            result = await helper({'action': action, 'username': username})
            await audit(q.from_user.id, 'proxy_' + action, username)
        except Exception:
            await error(q.message); return
        if action == 'reset':
            await credentials(q.message, result)
        else:
            await q.message.answer('دسترسی قطع شد. اتصال‌های باز حداکثر تا حدود یک ثانیه بسته می‌شوند.', reply_markup=home_keyboard())

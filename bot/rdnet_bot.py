import asyncio
import base64
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BOT_TOKEN = os.environ["BOT_TOKEN"]
PANEL_URL = os.environ["PANEL_URL"].rstrip("/")
PANEL_TOKEN = os.environ["PANEL_TOKEN"]
OWNER_CODE = os.environ["OWNER_BOOTSTRAP_CODE"]
DB_PATH = os.getenv("DB_PATH", "/var/lib/rdnet-bot/bot.db")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")
SUB_URL_BASE = os.getenv("SUB_URL_BASE", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rdnet-bot")
bot = Bot(BOT_TOKEN, session=AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None)
dp = Dispatcher()

T = {
 "fa": {"welcome":"به RDnet خوش آمدید. زبان را انتخاب کنید:","menu":"منوی اصلی","subs":"📊 اشتراک من","link":"🔗 اتصال اشتراک","renew":"♻️ درخواست تمدید","support":"💬 پشتیبانی","lang":"🌐 زبان","help_link":"نام اکانت و کد محرمانه را به این شکل بفرستید:\n<code>email UUID</code>","not_found":"اشتراک پیدا نشد یا کد محرمانه صحیح نیست.","linked":"اشتراک با موفقیت متصل شد.","no_sub":"هنوز اشتراکی به حساب شما متصل نشده است.","request":"درخواست شما ثبت شد و برای مدیر ارسال می‌شود.","owner":"شما به‌عنوان مدیر اصلی ثبت شدید.","denied":"دسترسی ندارید.","choose":"یکی از گزینه‌ها را انتخاب کنید.","panel_error":"در حال حاضر ارتباط با پنل ممکن نیست؛ بعداً دوباره تلاش کنید."},
 "en": {"welcome":"Welcome to RDnet. Choose your language:","menu":"Main menu","subs":"📊 My subscription","link":"🔗 Link subscription","renew":"♻️ Renewal request","support":"💬 Support","lang":"🌐 Language","help_link":"Send the account name and secret in this format:\n<code>email UUID</code>","not_found":"Subscription not found or the secret is invalid.","linked":"Subscription linked successfully.","no_sub":"No subscription is linked to your Telegram account.","request":"Your request was recorded and will be sent to an admin.","owner":"You are now registered as the owner.","denied":"Access denied.","choose":"Choose an option.","panel_error":"The panel is temporarily unavailable. Please try again later."}
}

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users(tg_id INTEGER PRIMARY KEY, lang TEXT NOT NULL DEFAULT 'fa', role TEXT NOT NULL DEFAULT 'user', created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS links(tg_id INTEGER NOT NULL, email TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, PRIMARY KEY(tg_id,email));
    CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT,tg_id INTEGER NOT NULL,kind TEXT NOT NULL,body TEXT,status TEXT NOT NULL DEFAULT 'pending',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS notices(tg_id INTEGER NOT NULL,email TEXT NOT NULL,kind TEXT NOT NULL,bucket TEXT NOT NULL,sent_at INTEGER NOT NULL,PRIMARY KEY(tg_id,email,kind,bucket));
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,tg_id INTEGER,action TEXT NOT NULL,detail TEXT,created_at INTEGER NOT NULL);
    """); c.commit()
    try: os.chmod(DB_PATH,0o600)
    except OSError: pass
    return c

def ensure_user(tg_id):
    with db() as c: c.execute("INSERT OR IGNORE INTO users(tg_id,created_at) VALUES(?,?)",(tg_id,int(time.time()))); c.commit()
def lang(tg_id):
    with db() as c:
        r=c.execute("SELECT lang FROM users WHERE tg_id=?",(tg_id,)).fetchone(); return r[0] if r else "fa"
def is_admin(tg_id):
    with db() as c:
        r=c.execute("SELECT role FROM users WHERE tg_id=?",(tg_id,)).fetchone(); return bool(r and r[0] in ("owner","admin"))
def menu(l):
    x=T[l]; return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=x["subs"],callback_data="subs"),InlineKeyboardButton(text=x["link"],callback_data="link")],[InlineKeyboardButton(text=x["renew"],callback_data="renew"),InlineKeyboardButton(text=x["support"],callback_data="support")],[InlineKeyboardButton(text=x["lang"],callback_data="language")]])

class VpnStates(StatesGroup):
    l2tp_name = State()
    l2tp_password = State()
    wg_name = State()
    v2ray_name = State()

def vpn_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 V2Ray",callback_data="vpn:v2ray")],
        [InlineKeyboardButton(text="🔐 L2TP",callback_data="vpn:l2tp"),InlineKeyboardButton(text="⚡ WireGuard",callback_data="vpn:wg")],
        [InlineKeyboardButton(text="🌐 پروکسی اختصاصی",callback_data="px:home")],
        [InlineKeyboardButton(text="🔄 به‌روزرسانی وضعیت",callback_data="vpn:status")]
    ])

def vpn_service_kb(service):
    if service == "v2ray":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ ساخت اکانت",callback_data="vpn:add:v2ray"),InlineKeyboardButton(text="👥 کاربران",callback_data="vpn:list:v2ray")],
            [InlineKeyboardButton(text="📊 وضعیت و مصرف",callback_data="vpn:status:v2ray")],
            [InlineKeyboardButton(text="⬅️ برگشت",callback_data="vpn:home")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ساخت کاربر",callback_data=f"vpn:add:{service}"),InlineKeyboardButton(text="👥 کاربران",callback_data=f"vpn:list:{service}")],
        [InlineKeyboardButton(text="📊 وضعیت و مصرف",callback_data=f"vpn:status:{service}")],
        [InlineKeyboardButton(text="⬅️ برگشت",callback_data="vpn:home")]
    ])

def human_bytes(value):
    value=float(value or 0)
    for unit in ("B","KB","MB","GB","TB"):
        if value < 1024 or unit == "TB": return f"{value:.1f} {unit}"
        value /= 1024

async def vpn_helper(*args):
    def call():
        p=subprocess.run(["sudo","-n","/usr/local/sbin/rdnet-vpn-helper",*args],capture_output=True,text=True,timeout=30)
        raw=(p.stdout or p.stderr).strip()
        try: data=__import__("json").loads(raw)
        except Exception: raise RuntimeError(raw or "VPN helper failed")
        if p.returncode or not data.get("ok"): raise RuntimeError(data.get("error") or "VPN helper failed")
        return data
    return await asyncio.to_thread(call)

async def audit(tg_id,action,detail=""):
    with db() as c:
        c.execute("INSERT INTO audit(tg_id,action,detail,created_at) VALUES(?,?,?,?)",(tg_id,action,detail,int(time.time()))); c.commit()

async def show_vpn_home(target):
    text="🛡 <b>مدیریت VPN سرور</b>\n\nاکانت‌های V2Ray، L2TP و WireGuard را ساده و دکمه‌ای مدیریت کنید."
    await target.answer(text,reply_markup=vpn_main_kb())

class Panel:
    def __init__(self): self.client=httpx.AsyncClient(base_url=PANEL_URL,headers={"Authorization":f"Bearer {PANEL_TOKEN}"},timeout=15,verify=True)
    async def get(self,path):
        r=await self.client.get(path); r.raise_for_status(); d=r.json()
        if not d.get("success"): raise RuntimeError(d.get("msg","panel error"))
        return d.get("obj")
    async def post(self,path,data=None):
        r=await self.client.post(path,json=data); r.raise_for_status(); d=r.json()
        if not d.get("success"): raise RuntimeError(d.get("msg","panel error"))
        return d.get("obj")
    async def clients(self): return await self.get("/panel/api/clients/list")
panel=Panel()

def find_client(items,email,secret=None):
    e=email.strip().lower()
    for x in items:
        if str(x.get("email","")).lower()==e:
            valid={str(x.get(k) or "") for k in ("uuid","id","subId","password","secret")}
            if secret is None or secrets.compare_digest(secret, next((v for v in valid if secrets.compare_digest(secret,v)),"")): return x
    return None

def fmt_client(x,l):
    tr=x.get("traffic") or {}; total=int(x.get("totalGB") or tr.get("total") or 0); used=int(tr.get("up") or 0)+int(tr.get("down") or 0); rem=max(0,total-used)
    gb=lambda n:f"{n/1073741824:.2f} GB"
    exp=int(x.get("expiryTime") or tr.get("expiryTime") or 0)
    ds="Unlimited / نامحدود" if exp<=0 else datetime.fromtimestamp(exp/1000,tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    status="✅" if x.get("enable",True) and (not tr or tr.get("enable",True)) else "⛔"
    if l=="fa": return f"{status} <b>{html.escape(str(x.get('email')))}</b>\nمصرف: {gb(used)}\nباقی‌مانده: {gb(rem) if total else 'نامحدود'}\nپایان: {ds}"
    return f"{status} <b>{html.escape(str(x.get('email')))}</b>\nUsed: {gb(used)}\nRemaining: {gb(rem) if total else 'Unlimited'}\nExpires: {ds}"

@dp.message(Command("start"))
async def start(m:Message):
    ensure_user(m.from_user.id)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="فارسی",callback_data="setlang:fa"),InlineKeyboardButton(text="English",callback_data="setlang:en")]])
    await m.answer("به RDnet خوش آمدید | Welcome to RDnet",reply_markup=kb)

@dp.message(Command("claimowner"))
async def claim_owner(m:Message):
    ensure_user(m.from_user.id); parts=(m.text or "").split(maxsplit=1)
    with db() as c:
        exists=c.execute("SELECT 1 FROM users WHERE role='owner'").fetchone()
        if exists or len(parts)!=2 or not secrets.compare_digest(parts[1],OWNER_CODE): await m.answer(T[lang(m.from_user.id)]["denied"]); return
        c.execute("UPDATE users SET role='owner' WHERE tg_id=?",(m.from_user.id,)); c.execute("INSERT INTO audit(tg_id,action,created_at) VALUES(?,?,?)",(m.from_user.id,"claim_owner",int(time.time()))); c.commit()
    await m.answer(T[lang(m.from_user.id)]["owner"])

@dp.callback_query(F.data.startswith("setlang:"))
async def set_lang(q:CallbackQuery):
    l=q.data.split(":",1)[1]
    ensure_user(q.from_user.id)
    with db() as c: c.execute("UPDATE users SET lang=? WHERE tg_id=?",(l,q.from_user.id)); c.commit()
    await q.message.edit_text(T[l]["menu"],reply_markup=menu(l)); await q.answer()

@dp.callback_query(F.data=="language")
async def language(q):
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="فارسی",callback_data="setlang:fa"),InlineKeyboardButton(text="English",callback_data="setlang:en")]])
    await q.message.answer(T[lang(q.from_user.id)]["welcome"],reply_markup=kb); await q.answer()

@dp.callback_query(F.data=="link")
async def link_help(q): await q.message.answer(T[lang(q.from_user.id)]["help_link"]); await q.answer()

@dp.message(Command("vpnadmin"))
async def vpn_admin_command(m:Message):
    if not is_admin(m.from_user.id): await m.answer(T[lang(m.from_user.id)]["denied"]); return
    await show_vpn_home(m)

@dp.callback_query(F.data=="vpn:home")
async def vpn_home(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    await state.clear(); await show_vpn_home(q.message); await q.answer()

@dp.callback_query(F.data.in_({"vpn:v2ray","vpn:l2tp","vpn:wg"}))
async def vpn_service(q:CallbackQuery):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    service=q.data.split(":")[1]; title={"v2ray":"V2Ray","l2tp":"L2TP","wg":"WireGuard"}[service]
    await q.message.answer(f"<b>{title}</b>\nعملیات موردنظر را انتخاب کنید:",reply_markup=vpn_service_kb(service)); await q.answer()

@dp.callback_query(F.data.startswith("vpn:add:"))
async def vpn_add_start(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    service=q.data.rsplit(":",1)[1]
    if service=="v2ray":
        await state.set_state(VpnStates.v2ray_name); await q.message.answer("نام اکانت V2Ray را بفرستید؛ مثلاً نام مشتری:")
    elif service=="l2tp":
        await state.set_state(VpnStates.l2tp_name); await q.message.answer("نام کاربر L2TP را بفرستید. فقط حروف انگلیسی، عدد، نقطه، خط تیره یا زیرخط:")
    else:
        await state.set_state(VpnStates.wg_name); await q.message.answer("نام کاربر WireGuard را بفرستید:")
    await q.answer()

@dp.message(VpnStates.v2ray_name)
async def v2ray_name(m:Message,state:FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    name=(m.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}",name): await m.answer("نام نامعتبر است؛ فقط حروف انگلیسی، عدد، نقطه، خط تیره یا زیرخط بفرستید."); return
    try:
        if find_client(await panel.clients(),name): await m.answer("این نام قبلاً وجود دارد؛ یک نام دیگر بفرستید."); return
    except Exception: await m.answer(T[lang(m.from_user.id)]["panel_error"]); await state.clear(); return
    await state.update_data(name=name); await state.set_state(None)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="50 GB",callback_data="v2:gb:50"),InlineKeyboardButton(text="100 GB",callback_data="v2:gb:100")],
        [InlineKeyboardButton(text="200 GB",callback_data="v2:gb:200"),InlineKeyboardButton(text="♾ نامحدود",callback_data="v2:gb:0")],
        [InlineKeyboardButton(text="لغو",callback_data="vpn:v2ray")]])
    await m.answer(f"حجم اکانت <b>{html.escape(name)}</b> را انتخاب کنید:",reply_markup=kb)

@dp.callback_query(F.data.startswith("v2:gb:"))
async def v2ray_gb(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    await state.update_data(gb=int(q.data.rsplit(":",1)[1]))
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="30 روز",callback_data="v2:days:30"),InlineKeyboardButton(text="60 روز",callback_data="v2:days:60")],
        [InlineKeyboardButton(text="90 روز",callback_data="v2:days:90"),InlineKeyboardButton(text="♾ نامحدود",callback_data="v2:days:0")]])
    await q.message.answer("مدت اعتبار را انتخاب کنید:",reply_markup=kb); await q.answer()

@dp.callback_query(F.data.startswith("v2:days:"))
async def v2ray_days(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    await state.update_data(days=int(q.data.rsplit(":",1)[1]))
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 دستگاه",callback_data="v2:limit:1"),InlineKeyboardButton(text="2 دستگاه",callback_data="v2:limit:2")],
        [InlineKeyboardButton(text="♾ نامحدود",callback_data="v2:limit:0")]])
    await q.message.answer("تعداد دستگاه هم‌زمان را انتخاب کنید:",reply_markup=kb); await q.answer()

def subscription_url(sub_id):
    if SUB_URL_BASE: return SUB_URL_BASE.rstrip("/")+"/"+sub_id
    u=urlsplit(PANEL_URL); return f"https://{u.hostname}:2096/sub/{sub_id}"

def qr_png(value):
    p=subprocess.run(["qrencode","-t","PNG","-o","-"],input=value.encode(),capture_output=True,timeout=10)
    if p.returncode: raise RuntimeError("QR generation failed")
    return p.stdout

@dp.callback_query(F.data.startswith("v2:limit:"))
async def v2ray_create_wizard(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    data=await state.get_data(); name=data.get("name"); gb=int(data.get("gb",0)); days=int(data.get("days",0)); limit=int(q.data.rsplit(":",1)[1])
    if not name: await q.message.answer("فرایند منقضی شده؛ دوباره ساخت اکانت را بزنید."); await q.answer(); return
    await q.message.answer("⏳ در حال ساخت اکانت…")
    try:
        rows=await panel.get("/panel/api/inbounds/options")
        inbound=next((x for x in rows if x.get("protocol")=="vless" and x.get("remark")!="rdnet-bot-local-socks"),None)
        if not inbound: raise RuntimeError("VLESS inbound not found")
        expiry=int((time.time()+days*86400)*1000) if days else 0
        body={"client":{"email":name,"totalGB":gb*1073741824,"expiryTime":expiry,"tgId":0,"limitIp":limit,"limitHwid":0,"enable":True},"inboundIds":[int(inbound["id"])]}
        await panel.post("/panel/api/clients/add",body)
        x=find_client(await panel.clients(),name)
        if not x: raise RuntimeError("created account could not be read")
        sub_id=str(x.get("subId") or ""); link=subscription_url(sub_id) if sub_id else ""
        await audit(q.from_user.id,"v2ray_add",name)
        summary=f"✅ <b>اکانت ساخته شد</b>\n\nنام: <code>{html.escape(name)}</code>\nحجم: {'نامحدود' if gb==0 else str(gb)+' GB'}\nمدت: {'نامحدود' if days==0 else str(days)+' روز'}\nدستگاه: {'نامحدود' if limit==0 else str(limit)}"
        if link:
            await q.message.answer_photo(BufferedInputFile(qr_png(link),filename=f"{name}-subscription.png"),caption=summary+f"\n\nلینک سابسکریپشن:\n<code>{html.escape(link)}</code>")
            await q.message.answer("برای کپی آسان، روی لینک زیر بزنید:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 بازکردن سابسکریپشن",url=link)],[InlineKeyboardButton(text="⬅️ مدیریت V2Ray",callback_data="vpn:v2ray")]]))
        else: await q.message.answer(summary+"\n\n⚠️ Sub ID ساخته نشد.",reply_markup=vpn_service_kb("v2ray"))
    except Exception as e:
        log.exception("V2Ray wizard failed"); await q.message.answer(f"❌ ساخت اکانت انجام نشد:\n<code>{html.escape(str(e))}</code>",reply_markup=vpn_service_kb("v2ray"))
    finally: await state.clear(); await q.answer()

@dp.message(VpnStates.l2tp_name)
async def l2tp_name(m:Message,state:FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    name=(m.text or "").strip()
    if not __import__("re").fullmatch(r"[A-Za-z0-9._-]{1,32}",name): await m.answer("نام نامعتبر است؛ دوباره بفرستید."); return
    await state.update_data(name=name); await state.set_state(VpnStates.l2tp_password)
    await m.answer("رمز کاربر را بفرستید. رمز نباید فاصله، کوتیشن یا بک‌اسلش داشته باشد. پیام رمز بعد از ساخت حذف می‌شود.")

@dp.message(VpnStates.l2tp_password)
async def l2tp_password(m:Message,state:FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    password=(m.text or "").strip(); data=await state.get_data(); name=data.get("name","")
    if not password or len(password)>128 or any(x in password for x in (' ', '"', "'", '\\')): await m.answer("رمز نامعتبر است؛ رمز دیگری بفرستید."); return
    try:
        await vpn_helper("l2tp-add",name,password); await audit(m.from_user.id,"l2tp_add",name)
        try: await m.delete()
        except Exception: pass
        await m.answer(f"✅ کاربر L2TP ساخته شد.\n\nنام: <code>{html.escape(name)}</code>\nرمز: <code>{html.escape(password)}</code>",reply_markup=vpn_service_kb("l2tp"))
    except Exception as e: await m.answer(f"❌ ساخت کاربر انجام نشد:\n<code>{html.escape(str(e))}</code>")
    finally: await state.clear()

@dp.message(VpnStates.wg_name)
async def wg_name(m:Message,state:FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    name=(m.text or "").strip()
    if not __import__("re").fullmatch(r"[A-Za-z0-9._-]{1,32}",name): await m.answer("نام نامعتبر است؛ دوباره بفرستید."); return
    try:
        data=await vpn_helper("wg-add",name); await audit(m.from_user.id,"wg_add",name)
        await m.answer_photo(BufferedInputFile(__import__("base64").b64decode(data["qr"]),filename=f"{name}.png"),caption=f"✅ WireGuard <b>{html.escape(name)}</b> ساخته شد. QR را با برنامه WireGuard اسکن کنید.")
        await m.answer_document(BufferedInputFile(data["config"].encode(),filename=f"{name}.conf"),caption="فایل کانفیگ آمادهٔ Import",reply_markup=vpn_service_kb("wg"))
    except Exception as e: await m.answer(f"❌ ساخت کاربر انجام نشد:\n<code>{html.escape(str(e))}</code>")
    finally: await state.clear()

@dp.callback_query(F.data.startswith("vpn:list:"))
async def vpn_list(q:CallbackQuery):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    service=q.data.rsplit(":",1)[1]
    if service=="v2ray":
        try:
            users=await panel.clients(); text="\n\n".join(fmt_client(x,"fa") for x in users)
            rows=[[InlineKeyboardButton(text=f"🗑 {x.get('email')}",callback_data=f"vpn:askdel:v2ray:{x.get('email')}")] for x in users if len(str(x.get('email','')))<=32]
            rows.append([InlineKeyboardButton(text="⬅️ برگشت",callback_data="vpn:v2ray")])
            await q.message.answer(text or "اکانت V2Ray ساخته نشده است.",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception as e: await q.message.answer(f"❌ <code>{html.escape(str(e))}</code>")
        await q.answer(); return
    try: users=(await vpn_helper(f"{service}-list"))["users"]
    except Exception as e: await q.message.answer(f"❌ <code>{html.escape(str(e))}</code>"); await q.answer(); return
    rows=[[InlineKeyboardButton(text=f"🗑 {name}",callback_data=f"vpn:askdel:{service}:{name}")] for name in users]
    rows.append([InlineKeyboardButton(text="⬅️ برگشت",callback_data=f"vpn:{service}")])
    await q.message.answer(("کاربری ساخته نشده است." if not users else "برای حذف، نام کاربر را بزنید:"),reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await q.answer()

@dp.callback_query(F.data.startswith("vpn:askdel:"))
async def vpn_ask_delete(q:CallbackQuery):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    _,_,service,name=q.data.split(":",3)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="بله، حذف شود",callback_data=f"vpn:del:{service}:{name}"),InlineKeyboardButton(text="لغو",callback_data=f"vpn:{service}")]])
    await q.message.answer(f"⚠️ کاربر <b>{html.escape(name)}</b> حذف شود؟",reply_markup=kb); await q.answer()

@dp.callback_query(F.data.startswith("vpn:del:"))
async def vpn_delete(q:CallbackQuery):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    _,_,service,name=q.data.split(":",3)
    try:
        if service=="v2ray": await panel.post(f"/panel/api/clients/del/{name}")
        else: await vpn_helper(f"{service}-del",name)
        await audit(q.from_user.id,f"{service}_del",name)
        await q.message.answer(f"✅ <b>{html.escape(name)}</b> حذف شد.",reply_markup=vpn_service_kb(service))
    except Exception as e: await q.message.answer(f"❌ <code>{html.escape(str(e))}</code>")
    await q.answer()

@dp.callback_query(F.data=="vpn:status")
async def vpn_all_status(q:CallbackQuery):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    await render_vpn_status(q,"l2tp"); await render_vpn_status(q,"wg"); await q.answer()

@dp.callback_query(F.data.startswith("vpn:status:"))
async def vpn_one_status(q:CallbackQuery):
    if not is_admin(q.from_user.id): await q.answer("دسترسی ندارید",show_alert=True); return
    await render_vpn_status(q,q.data.rsplit(":",1)[1]); await q.answer()

async def render_vpn_status(q,service):
    if service=="v2ray":
        try: users=await panel.clients(); text="\n\n".join(fmt_client(x,"fa") for x in users)
        except Exception as e: await q.message.answer(f"❌ V2Ray: <code>{html.escape(str(e))}</code>"); return
        await q.message.answer(text or "اکانت V2Ray ساخته نشده است.",reply_markup=vpn_service_kb("v2ray")); return
    title="L2TP" if service=="l2tp" else "WireGuard"
    try: users=(await vpn_helper(f"{service}-status"))["users"]
    except Exception as e: await q.message.answer(f"❌ {title}: <code>{html.escape(str(e))}</code>"); return
    lines=[f"<b>📊 {title}</b>"]
    for x in users:
        status="🟢 آنلاین" if x["online"] else "⚪️ آفلاین"
        traffic=f"↓ {human_bytes(x['rx'])}  ↑ {human_bytes(x['tx'])}"
        extra=""
        if service=="l2tp" and x.get("connected"): extra=f" · {x['connected']//60} دقیقه"
        if service=="wg" and x.get("handshake"): extra=f" · آخرین اتصال {max(0,int(time.time())-x['handshake'])//60} دقیقه قبل"
        lines.append(f"\n<b>{html.escape(x['name'])}</b> — {status}{extra}\n{traffic}")
    await q.message.answer("\n".join(lines) if users else f"<b>{title}</b>\nکاربری ساخته نشده است.",reply_markup=vpn_service_kb(service))

from rdnet_proxy_ui import register_proxy_ui
register_proxy_ui(dp, is_admin, audit)

@dp.message(F.text.regexp(r"^[^\s]{1,128}\s+[^\s]{8,256}$"))
async def link_account(m:Message):
    ensure_user(m.from_user.id); l=lang(m.from_user.id); email,secret=(m.text or "").split(maxsplit=1)
    try: x=find_client(await panel.clients(),email,secret)
    except Exception: log.exception("panel lookup failed"); await m.answer(T[l]["panel_error"]); return
    if not x: await m.answer(T[l]["not_found"]); return
    with db() as c:
        try: c.execute("INSERT INTO links(tg_id,email,created_at) VALUES(?,?,?)",(m.from_user.id,x["email"],int(time.time()))); c.commit()
        except sqlite3.IntegrityError: await m.answer(T[l]["not_found"]); return
    await m.answer(T[l]["linked"],reply_markup=menu(l))

@dp.callback_query(F.data=="subs")
async def subscriptions(q):
    l=lang(q.from_user.id)
    with db() as c: emails=[r[0] for r in c.execute("SELECT email FROM links WHERE tg_id=?",(q.from_user.id,))]
    if not emails: await q.message.answer(T[l]["no_sub"]); await q.answer(); return
    try: items=await panel.clients(); text="\n\n".join(fmt_client(x,l) for e in emails if (x:=find_client(items,e)))
    except Exception: text=T[l]["panel_error"]
    await q.message.answer(text or T[l]["no_sub"]); await q.answer()

async def make_request(q:CallbackQuery,kind):
    l=lang(q.from_user.id)
    with db() as c: c.execute("INSERT INTO requests(tg_id,kind,body,created_at) VALUES(?,?,?,?)",(q.from_user.id,kind,"",int(time.time()))); c.commit(); admins=[r[0] for r in c.execute("SELECT tg_id FROM users WHERE role IN ('owner','admin')")]
    for a in admins:
        try: await bot.send_message(a,f"New {kind} request / درخواست جدید\nTelegram ID: <code>{q.from_user.id}</code>")
        except Exception: log.warning("admin notification failed")
    await q.message.answer(T[l]["request"]); await q.answer()
@dp.callback_query(F.data=="renew")
async def renew(q): await make_request(q,"renew")
@dp.callback_query(F.data=="support")
async def support(q): await make_request(q,"support")

@dp.message(Command("admin"))
async def admin(m:Message):
    if not is_admin(m.from_user.id): await m.answer(T[lang(m.from_user.id)]["denied"]); return
    with db() as c:
        u=c.execute("SELECT count(*) FROM users").fetchone()[0]; r=c.execute("SELECT count(*) FROM requests WHERE status='pending'").fetchone()[0]; ln=c.execute("SELECT count(*) FROM links").fetchone()[0]
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛡 مدیریت V2Ray، L2TP و WireGuard",callback_data="vpn:home")]])
    await m.answer(f"RDnet Admin\nUsers: {u}\nLinked accounts: {ln}\nPending requests: {r}\n\n/requestlist · /inbounds · /health",reply_markup=kb)

@dp.message(Command("health"))
async def health(m:Message):
    if not is_admin(m.from_user.id): return
    try:
        items=await panel.clients()
        await m.answer(f"✅ Telegram: OK\n✅ 3X-UI API: OK\nClients: {len(items)}\nScheduler: every {CHECK_INTERVAL//60} min")
    except Exception:
        await m.answer("✅ Telegram: OK\n❌ 3X-UI API: unavailable")

@dp.message(Command("inbounds"))
async def inbounds(m:Message):
    if not is_admin(m.from_user.id): return
    try:
        rows=await panel.get("/panel/api/inbounds/options")
        text="\n".join(f"<code>{x.get('id')}</code> · {html.escape(str(x.get('remark')))} · {x.get('protocol')}:{x.get('port')}" for x in rows if x.get('remark')!='rdnet-bot-local-socks')
        await m.answer(text or "No inbounds")
    except Exception: await m.answer(T[lang(m.from_user.id)]["panel_error"])

@dp.message(Command("requestlist"))
async def reqs(m:Message):
    if not is_admin(m.from_user.id): return
    with db() as c: rows=c.execute("SELECT id,tg_id,kind,created_at FROM requests WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall()
    await m.answer("\n".join(f"#{r['id']} · {r['kind']} · <code>{r['tg_id']}</code>" for r in rows) or "No pending requests")

async def resolve_request(m:Message,status:str):
    if not is_admin(m.from_user.id): return
    parts=(m.text or "").split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer(f"Usage: /{status} request_id"); return
    with db() as c:
        row=c.execute("SELECT id,tg_id,kind FROM requests WHERE id=? AND status='pending'",(int(parts[1]),)).fetchone()
        if not row: await m.answer("Request not found or already processed."); return
        c.execute("UPDATE requests SET status=? WHERE id=?",(status,row['id']))
        c.execute("INSERT INTO audit(tg_id,action,detail,created_at) VALUES(?,?,?,?)",(m.from_user.id,f"request_{status}",str(row['id']),int(time.time()))); c.commit()
    ul=lang(row['tg_id']); msg=("✅ درخواست شما تأیید شد." if status=="approved" else "❌ درخواست شما رد شد.") if ul=="fa" else ("✅ Your request was approved." if status=="approved" else "❌ Your request was rejected.")
    try: await bot.send_message(row['tg_id'],msg)
    except Exception: log.warning("request result notification failed")
    await m.answer(f"Request #{row['id']} → {status}")

@dp.message(Command("approve"))
async def approve(m:Message): await resolve_request(m,"approved")
@dp.message(Command("reject"))
async def reject(m:Message): await resolve_request(m,"rejected")

@dp.message(Command("create"))
async def create_client(m:Message):
    if not is_admin(m.from_user.id):
        await m.answer(T[lang(m.from_user.id)]["denied"]); return
    parts=(m.text or "").split()
    if len(parts) not in (5,6):
        await m.answer("Usage:\n<code>/create email GB days inbound_id [limit_ip]</code>\nExample: <code>/create ali 100 30 1 2</code>"); return
    try:
        email=parts[1].strip(); gb=int(parts[2]); days=int(parts[3]); inbound_id=int(parts[4]); limit_ip=int(parts[5]) if len(parts)==6 else 2
        if not (1<=len(email)<=128 and 1<=gb<=10000 and 1<=days<=730 and inbound_id>0 and 0<=limit_ip<=100): raise ValueError
        if find_client(await panel.clients(),email): await m.answer("این نام قبلاً وجود دارد / Account already exists."); return
        expiry=int((time.time()+days*86400)*1000)
        body={"client":{"email":email,"totalGB":gb*1073741824,"expiryTime":expiry,"tgId":0,"limitIp":limit_ip,"limitHwid":0,"enable":True},"inboundIds":[inbound_id]}
        await panel.post("/panel/api/clients/add",body)
        x=find_client(await panel.clients(),email)
        if not x: raise RuntimeError("created client could not be read back")
        secret=x.get("uuid") or x.get("id") or x.get("password") or x.get("secret")
        await m.answer(f"✅ Created\nEmail: <code>{html.escape(email)}</code>\nSecret: <code>{html.escape(str(secret))}</code>\nSub ID: <code>{html.escape(str(x.get('subId') or '-'))}</code>\n{fmt_client(x,lang(m.from_user.id))}")
        with db() as c: c.execute("INSERT INTO audit(tg_id,action,detail,created_at) VALUES(?,?,?,?)",(m.from_user.id,"create_client",email,int(time.time()))); c.commit()
    except ValueError: await m.answer("پارامترها نامعتبرند / Invalid parameters.")
    except Exception: log.exception("create client failed"); await m.answer(T[lang(m.from_user.id)]["panel_error"])

async def monitor():
    await asyncio.sleep(5)
    while True:
        try:
            items=await panel.clients(); now=int(time.time()*1000)
            with db() as c: links=c.execute("SELECT tg_id,email FROM links").fetchall()
            for row in links:
                x=find_client(items,row["email"])
                if not x: continue
                tr=x.get("traffic") or {}; total=int(x.get("totalGB") or tr.get("total") or 0); used=int(tr.get("up") or 0)+int(tr.get("down") or 0); exp=int(x.get("expiryTime") or tr.get("expiryTime") or 0)
                checks=[]
                l=lang(row["tg_id"])
                if total>0:
                    pct=max(0,(total-used)*100/total)
                    for v in (20,10,5,0):
                        if pct<=v:
                            text=f"⚠️ {row['email']}: فقط {pct:.1f}٪ حجم باقی مانده است." if l=="fa" else f"⚠️ {row['email']}: {pct:.1f}% traffic remaining."
                            checks.append(("traffic",str(v),text)); break
                if exp>0:
                    hours=(exp-now)/3600000
                    for v in (168,72,24,6,0):
                        if hours<=v:
                            text=f"⚠️ {row['email']}: حدود {max(0,hours):.0f} ساعت تا پایان اشتراک مانده است." if l=="fa" else f"⚠️ {row['email']}: {max(0,hours):.0f} hours remaining."
                            checks.append(("expiry",str(v),text)); break
                for kind,bucket,text in checks:
                    try:
                        c.execute("INSERT INTO notices(tg_id,email,kind,bucket,sent_at) VALUES(?,?,?,?,?)",(row["tg_id"],row["email"],kind,bucket,int(time.time()))); c.commit(); await bot.send_message(row["tg_id"],text)
                    except sqlite3.IntegrityError: pass
        except Exception: log.exception("monitor cycle failed")
        await asyncio.sleep(CHECK_INTERVAL)

async def main():
    asyncio.create_task(monitor())
    await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
if __name__=="__main__": asyncio.run(main())

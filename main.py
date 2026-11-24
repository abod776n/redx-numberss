"""RedX Numbers Safe — API provider manager only.
This standalone bot manages generic provider connections. It does not collect Telegram
login codes, create user sessions, or perform account authorization/session operations.
"""
import asyncio
import ipaddress
import json
import os
import socket
import sqlite3
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cryptography.fernet import Fernet, InvalidToken

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
FERNET_KEY = os.getenv("CONFIG_ENCRYPTION_KEY", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "redx_providers.db")
if not BOT_TOKEN or not ADMIN_IDS or not FERNET_KEY:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN, ADMIN_IDS and CONFIG_ENCRYPTION_KEY")
fernet = Fernet(FERNET_KEY.encode())

class AddProvider(StatesGroup):
    name = State()
    url = State()
    auth_mode = State()
    api_key = State()
    services_path = State()
    balance_path = State()


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS providers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, base_url TEXT NOT NULL,
            auth_mode TEXT NOT NULL, secret BLOB NOT NULL, services_path TEXT NOT NULL,
            balance_path TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ إضافة مزوّد API", callback_data="add")
    kb.button(text="📋 مزوّدو API", callback_data="list")
    kb.adjust(1)
    return kb.as_markup()


def safe_base_url(value: str) -> str:
    p = urlparse(value.strip())
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("استخدم رابط HTTPS صحيحاً بدون بيانات دخول داخل الرابط.")
    host = p.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("العنوان المحلي غير مسموح.")
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("عناوين الشبكات الداخلية غير مسموحة.")
    except ValueError as e:
        if "غير مسموح" in str(e):
            raise
        try:
            infos = socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM)
        except OSError:
            raise ValueError("تعذر التحقق من عنوان الموقع.")
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if not addr.is_global:
                raise ValueError("الموقع يحيل إلى عنوان شبكة داخلية وغير مسموح.")
    return value.strip().rstrip("/")


async def request_provider(base_url: str, path: str, mode: str, secret: str):
    base_url = safe_base_url(base_url)
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("مسار API يجب أن يبدأ بشرطة واحدة /")
    url = base_url + path
    headers = {"Accept": "application/json"}
    if mode == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif mode == "x-api-key":
        headers["X-API-Key"] = secret
    elif mode.startswith("custom:"):
        headers[mode.split(":", 1)[1]] = secret
    timeout = aiohttp.ClientTimeout(total=15)
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as s:
        async with s.get(url, headers=headers, allow_redirects=False) as r:
            body = await r.text()
            if 300 <= r.status < 400:
                raise ValueError("الموقع أعاد تحويلًا؛ تم إيقافه للحماية.")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body[:1500]
            if r.status >= 400:
                detail = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)[:700]
                raise ValueError(f"HTTP {r.status}: {detail}")
            return r.status, data


async def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(m: Message):
        if not is_admin(m.from_user.id):
            return await m.answer("غير مصرح لك باستخدام لوحة الإدارة.")
        await m.answer(
            "🔌 **RedX Numbers | إدارة مزوّدي API**\n\nأضف مزوّداً واختبر الاتصال واستعرض الخدمات والرصيد.",
            reply_markup=menu(), parse_mode="Markdown"
        )

    @dp.callback_query(F.data == "list")
    async def list_providers(q: CallbackQuery):
        if not is_admin(q.from_user.id):
            return await q.answer("غير مصرح", show_alert=True)
        with db() as c:
            rows = c.execute(
                "SELECT id,name,base_url,auth_mode,services_path,balance_path FROM providers ORDER BY id DESC"
            ).fetchall()
        if not rows:
            await q.message.edit_text("لا يوجد مزوّدون محفوظون حالياً.", reply_markup=menu())
            return await q.answer()
        kb = InlineKeyboardBuilder()
        lines = ["📋 **المزوّدون المحفوظون**"]
        for pid, name, url, mode, sp, bp in rows:
            lines.append(f"\n**{pid}. {name}**\n{url}\nالمصادقة: {mode} | الخدمات: `{sp}` | الرصيد: `{bp}`")
            kb.button(text=f"🧪 اختبار {name}", callback_data=f"test:{pid}")
        kb.button(text="⬅️ القائمة", callback_data="home")
        kb.adjust(1)
        await q.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="Markdown")
        await q.answer()

    @dp.callback_query(F.data == "home")
    async def home(q: CallbackQuery):
        if is_admin(q.from_user.id):
            await q.message.edit_text("لوحة إدارة مزوّدي API", reply_markup=menu())
        await q.answer()

    @dp.callback_query(F.data == "add")
    async def add(q: CallbackQuery, state: FSMContext):
        if not is_admin(q.from_user.id):
            return await q.answer("غير مصرح", show_alert=True)
        await state.clear()
        await state.set_state(AddProvider.name)
        await q.message.answer("أرسل اسم المزوّد:")
        await q.answer()

    @dp.message(AddProvider.name)
    async def add_name(m: Message, state: FSMContext):
        await state.update_data(name=(m.text or "").strip()[:80])
        await state.set_state(AddProvider.url)
        await m.answer("أرسل رابط API الأساسي (HTTPS)، مثال: https://api.example.com/v1")

    @dp.message(AddProvider.url)
    async def add_url(m: Message, state: FSMContext):
        try:
            url = safe_base_url(m.text or "")
        except Exception as e:
            return await m.answer(f"❌ {e}\nأرسل الرابط مرة أخرى.")
        await state.update_data(url=url)
        await state.set_state(AddProvider.auth_mode)
        kb = InlineKeyboardBuilder()
        kb.button(text="Bearer Token", callback_data="auth:bearer")
        kb.button(text="X-API-Key", callback_data="auth:x-api-key")
        kb.button(text="Custom Header", callback_data="auth:custom")
        kb.adjust(1)
        await m.answer("اختر طريقة المصادقة:", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("auth:"))
    async def auth_mode(q: CallbackQuery, state: FSMContext):
        if not is_admin(q.from_user.id):
            return await q.answer("غير مصرح", show_alert=True)
        mode = q.data.split(":", 1)[1]
        if mode == "custom":
            await q.message.answer("أرسل اسم ترويسة المصادقة، مثال: Authorization أو Api-Key")
            await state.set_state(AddProvider.auth_mode)
            await state.update_data(wait_custom=True)
        else:
            await state.update_data(auth_mode=mode)
            await state.set_state(AddProvider.api_key)
            await q.message.answer("أرسل API Key. احذف رسالتك بعد إرسالها لعدم إبقاء المفتاح في سجل المحادثة.")
        await q.answer()

    @dp.message(AddProvider.auth_mode)
    async def custom_header(m: Message, state: FSMContext):
        d = await state.get_data()
        if not d.get("wait_custom"):
            return
        header = (m.text or "").strip()
        if not header or len(header) > 80 or any(ch in header for ch in "\r\n:"):
            return await m.answer("اسم الترويسة غير صالح. أعد الإرسال.")
        await state.update_data(auth_mode="custom:" + header, wait_custom=False)
        await state.set_state(AddProvider.api_key)
        await m.answer("أرسل قيمة API Key. احذف رسالتك بعد إرسالها لعدم إبقاء المفتاح في سجل المحادثة.")

    @dp.message(AddProvider.api_key)
    async def add_key(m: Message, state: FSMContext):
        secret = (m.text or "").strip()
        if len(secret) < 4 or len(secret) > 500:
            return await m.answer("المفتاح غير صالح؛ أعد إرساله.")
        await state.update_data(secret=secret)
        try:
            await m.delete()
        except Exception:
            pass
        await state.set_state(AddProvider.services_path)
        await m.answer("أرسل مسار جلب الخدمات، مثال: /services (أو اكتب - للتخطي)")

    @dp.message(AddProvider.services_path)
    async def services_path(m: Message, state: FSMContext):
        p = (m.text or "").strip()
        p = "" if p == "-" else p
        if p and (not p.startswith("/") or p.startswith("//")):
            return await m.answer("المسار غير صحيح. أرسله مثل /services أو - للتخطي")
        await state.update_data(services_path=p)
        await state.set_state(AddProvider.balance_path)
        await m.answer("أرسل مسار فحص الرصيد، مثال: /balance (أو اكتب - للتخطي)")

    @dp.message(AddProvider.balance_path)
    async def balance_path(m: Message, state: FSMContext):
        p = (m.text or "").strip()
        p = "" if p == "-" else p
        if p and (not p.startswith("/") or p.startswith("//")):
            return await m.answer("المسار غير صحيح. أرسله مثل /balance أو - للتخطي")
        d = await state.get_data()
        with db() as c:
            c.execute(
                "INSERT INTO providers(name,base_url,auth_mode,secret,services_path,balance_path) VALUES(?,?,?,?,?,?)",
                (d["name"], d["url"], d["auth_mode"], fernet.encrypt(d["secret"].encode()), d["services_path"], p)
            )
        await state.clear()
        await m.answer("✅ تم حفظ المزوّد وتشفير المفتاح. يمكنك اختباره من قائمة المزوّدين.", reply_markup=menu())

    @dp.callback_query(F.data.startswith("test:"))
    async def test_provider(q: CallbackQuery):
        if not is_admin(q.from_user.id):
            return await q.answer("غير مصرح", show_alert=True)
        pid = int(q.data.split(":", 1)[1])
        with db() as c:
            row = c.execute(
                "SELECT name,base_url,auth_mode,secret,services_path,balance_path FROM providers WHERE id=?",
                (pid,)
            ).fetchone()
        if not row:
            return await q.answer("المزوّد غير موجود", show_alert=True)
        name, url, mode, secret, sp, bp = row
        try:
            key = fernet.decrypt(secret).decode()
            path = sp or bp
            if not path:
                await q.message.answer("تم الحفظ. أضف مسار الخدمات أو الرصيد حتى يمكن اختبار الاتصال.")
                return await q.answer()
            status, data = await request_provider(url, path, mode, key)
            preview = json.dumps(data, ensure_ascii=False, indent=2)[:2500]
            await q.message.answer(
                f"✅ اتصال ناجح مع **{name}**\nHTTP: `{status}`\nالمسار: `{path}`\n\n```json\n{preview}\n```",
                parse_mode="Markdown"
            )
        except Exception as e:
            await q.message.answer(f"❌ فشل اختبار **{name}**: {str(e)[:700]}", parse_mode="Markdown")
        await q.answer()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

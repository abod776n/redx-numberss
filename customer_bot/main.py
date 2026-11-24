import asyncio
import os
import re
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
if not TOKEN or not DATABASE_URL:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN and DATABASE_URL")

pool = None

class OrderFlow(StatesGroup):
    link = State()
    quantity = State()

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=20)
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_services (
            id BIGSERIAL PRIMARY KEY,
            provider_id BIGINT,
            external_service_id TEXT,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'عام',
            description TEXT NOT NULL DEFAULT '',
            price_per_1000 NUMERIC(18,6) NOT NULL DEFAULT 0,
            min_quantity BIGINT NOT NULL DEFAULT 1,
            max_quantity BIGINT NOT NULL DEFAULT 1000000,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(provider_id, external_service_id)
        )""")
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_orders (
            id BIGSERIAL PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL,
            telegram_username TEXT,
            service_id BIGINT NOT NULL REFERENCES provider_services(id),
            target_url TEXT NOT NULL,
            quantity BIGINT NOT NULL,
            quoted_price NUMERIC(18,6) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_review',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")

def home_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍️ تصفح الخدمات", callback_data="services")
    kb.button(text="📦 طلباتي", callback_data="myorders")
    kb.adjust(1)
    return kb.as_markup()

async def main():
    await init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(m: Message):
        await m.answer(
            "أهلاً بك في **طلباتي نمبر** 👋\n\n"
            "تصفح الخدمات المتاحة، أرسل طلبك، وتابع حالة طلباتك من هنا.",
            reply_markup=home_kb(), parse_mode="Markdown"
        )

    @dp.callback_query(F.data == "services")
    async def services(q: CallbackQuery):
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, name, category, description, price_per_1000, min_quantity, max_quantity
                FROM provider_services WHERE active=TRUE ORDER BY category, id LIMIT 50
            """)
        if not rows:
            await q.message.edit_text("لا توجد خدمات منشورة حالياً. جرّب لاحقاً.", reply_markup=home_kb())
            return await q.answer()
        kb = InlineKeyboardBuilder()
        lines = ["🛍️ **الخدمات المتاحة**", ""]
        for r in rows:
            lines.append(
                f"**#{r['id']} — {r['name']}**\n"
                f"القسم: {r['category']} | السعر لكل 1000: {r['price_per_1000']}\n"
                f"الكمية: {r['min_quantity']} - {r['max_quantity']}"
            )
            if r["description"]:
                lines.append(str(r["description"])[:180])
            lines.append("")
            kb.button(text=f"اطلب #{r['id']}", callback_data=f"order:{r['id']}")
        kb.button(text="⬅️ الرئيسية", callback_data="home")
        kb.adjust(1)
        await q.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="Markdown")
        await q.answer()

    @dp.callback_query(F.data == "home")
    async def home(q: CallbackQuery):
        await q.message.edit_text("**طلباتي نمبر**\nاختر من القائمة:", reply_markup=home_kb(), parse_mode="Markdown")
        await q.answer()

    @dp.callback_query(F.data.startswith("order:"))
    async def order_start(q: CallbackQuery, state: FSMContext):
        sid = int(q.data.split(":", 1)[1])
        async with pool.acquire() as conn:
            service = await conn.fetchrow(
                "SELECT id,name,min_quantity,max_quantity,price_per_1000 FROM provider_services WHERE id=$1 AND active=TRUE", sid
            )
        if not service:
            return await q.answer("الخدمة غير متاحة حالياً", show_alert=True)
        await state.clear()
        await state.update_data(service_id=sid, service_name=service["name"])
        await state.set_state(OrderFlow.link)
        await q.message.answer(f"أرسل رابط المنشور أو الحساب المطلوب لخدمة **{service['name']}**:", parse_mode="Markdown")
        await q.answer()

    @dp.message(OrderFlow.link)
    async def order_link(m: Message, state: FSMContext):
        link = (m.text or "").strip()
        if not re.match(r"^https?://[^\\s]+$", link, re.I) or len(link) > 500:
            return await m.answer("أرسل رابطاً صحيحاً يبدأ بـ https:// أو http://")
        await state.update_data(target_url=link)
        await state.set_state(OrderFlow.quantity)
        await m.answer("أرسل الكمية المطلوبة بالأرقام:")

    @dp.message(OrderFlow.quantity)
    async def order_quantity(m: Message, state: FSMContext):
        raw = (m.text or "").strip()
        if not raw.isdigit():
            return await m.answer("أرسل الكمية بالأرقام فقط.")
        qty = int(raw)
        data = await state.get_data()
        async with pool.acquire() as conn:
            service = await conn.fetchrow(
                "SELECT * FROM provider_services WHERE id=$1 AND active=TRUE", data["service_id"]
            )
            if not service or qty < service["min_quantity"] or qty > service["max_quantity"]:
                return await m.answer("الكمية خارج الحدود المسموحة لهذه الخدمة. أعد المحاولة.")
            price = round(float(service["price_per_1000"]) * qty / 1000, 6)
            order_id = await conn.fetchval("""
                INSERT INTO customer_orders
                (telegram_user_id,telegram_username,service_id,target_url,quantity,quoted_price)
                VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
            """, m.from_user.id, m.from_user.username, data["service_id"], data["target_url"], qty, price)
        await state.clear()
        await m.answer(
            f"✅ تم استلام طلبك رقم **#{order_id}**\n"
            f"الخدمة: {data['service_name']}\nالكمية: {qty}\n"
            f"التكلفة التقديرية: {price}\n\n"
            "الطلب قيد المراجعة، وسيتم تحديث حالته بعد مراجعته.",
            reply_markup=home_kb(), parse_mode="Markdown"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🆕 طلب جديد #{order_id}\nالعميل: {m.from_user.full_name} ({m.from_user.id})\n"
                    f"الخدمة: {data['service_name']}\nالكمية: {qty}\nالرابط: {data['target_url']}"
                )
            except Exception:
                pass

    @dp.callback_query(F.data == "myorders")
    async def myorders(q: CallbackQuery):
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT o.id,s.name,o.quantity,o.quoted_price,o.status,o.created_at
                FROM customer_orders o JOIN provider_services s ON s.id=o.service_id
                WHERE o.telegram_user_id=$1 ORDER BY o.id DESC LIMIT 10
            """, q.from_user.id)
        if not rows:
            msg = "لا توجد طلبات مسجلة لك حتى الآن."
        else:
            msg = "📦 **آخر طلباتك**\n\n" + "\n".join(
                f"#{r['id']} — {r['name']}\nالكمية: {r['quantity']} | التكلفة: {r['quoted_price']}\nالحالة: {r['status']}"
                for r in rows
            )
        await q.message.edit_text(msg, reply_markup=home_kb(), parse_mode="Markdown")
        await q.answer()

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if pool:
            asyncio.run(pool.close())

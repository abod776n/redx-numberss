import asyncio
import os
from decimal import Decimal, InvalidOperation
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
pool = None
YER_PER_USD = Decimal("560")

def yer(usd):
    return f"{Decimal(usd) * YER_PER_USD:,.2f} ر.ي"

class SupportFlow(StatesGroup):
    message = State()

def home_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 شراء رقم", callback_data="numbers")
    kb.button(text="💰 رصيدي", callback_data="balance")
    kb.button(text="📦 طلباتي", callback_data="myorders")
    kb.button(text="➕ شحن الرصيد", callback_data="topup")
    kb.button(text="📖 طريقة الاستخدام", callback_data="instructions")
    kb.button(text="📞 الدعم الفني", callback_data="support")
    kb.adjust(2, 2, 2)
    return kb.as_markup()

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=20)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS number_wallets(
            telegram_user_id BIGINT PRIMARY KEY,
            balance_usd NUMERIC(18,6) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        await c.execute("""
        CREATE TABLE IF NOT EXISTS number_transactions(
            id BIGSERIAL PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL,
            amount_usd NUMERIC(18,6) NOT NULL,
            kind TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        await c.execute("""
        CREATE TABLE IF NOT EXISTS telegram_star_payments(
            telegram_payment_charge_id TEXT PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL,
            stars INTEGER NOT NULL,
            credited_usd NUMERIC(18,6) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        await c.execute("""
        CREATE TABLE IF NOT EXISTS number_orders(
            id BIGSERIAL PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL,
            service_id BIGINT NOT NULL REFERENCES provider_services(id),
            service_name TEXT NOT NULL,
            price_usd NUMERIC(18,6) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_review',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")

async def ensure_wallet(uid):
    async with pool.acquire() as c:
        await c.execute("INSERT INTO number_wallets(telegram_user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
        return await c.fetchval("SELECT balance_usd FROM number_wallets WHERE telegram_user_id=$1", uid)

async def main():
    if not TOKEN or not DATABASE_URL:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and DATABASE_URL")
    await init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(m: Message):
        await ensure_wallet(m.from_user.id)
        await m.answer(
            "مرحباً بك في طلباتي نمبر 📱\n\n"
            "متجر لعرض الأرقام المتاحة وطلب شرائها. اختر شراء رقم لعرض الدول والأسعار.",
            reply_markup=home_kb()
        )

    @dp.callback_query(F.data == "home")
    async def home(q: CallbackQuery):
        await q.message.edit_text("طلباتي نمبر 📱\nاختر الخدمة المطلوبة:", reply_markup=home_kb())
        await q.answer()

    @dp.callback_query(F.data == "balance")
    async def balance(q: CallbackQuery):
        value = await ensure_wallet(q.from_user.id)
        await q.message.answer(f"💰 رصيدك الحالي: {yer(value)}", reply_markup=home_kb())
        await q.answer()

    @dp.callback_query(F.data == "topup")
    async def topup(q: CallbackQuery):
        kb = InlineKeyboardBuilder()
        for stars in (100, 250, 500, 1000):
            kb.button(text=f"⭐ {stars} نجمة = {yer(Decimal(stars) / Decimal(100))}", callback_data=f"starpack:{stars}")
        kb.button(text="⬅️ الرئيسية", callback_data="home")
        kb.adjust(1)
        await q.message.answer("⭐ شحن الرصيد عبر Telegram Stars\\nسعر التحويل: 100 نجمة = 560 ريال يمني. اختر الباقة:", reply_markup=kb.as_markup())
        await q.answer()

    @dp.callback_query(F.data.startswith("starpack:"))
    async def starpack(q: CallbackQuery):
        try:
            stars = int(q.data.split(":", 1)[1])
        except ValueError:
            return await q.answer("باقة غير صالحة", show_alert=True)
        if stars not in (100, 250, 500, 1000):
            return await q.answer("هذه الباقة غير متاحة", show_alert=True)
        await bot.send_invoice(chat_id=q.from_user.id, title=f"شحن طلباتي نمبر — {stars} نجمة", description=f"إضافة {yer(Decimal(stars) / Decimal(100))} إلى محفظتك", payload=f"wallet_topup:{q.from_user.id}:{stars}", provider_token="", currency="XTR", prices=[LabeledPrice(label=f"{stars} Telegram Stars", amount=stars)])
        await q.answer()

    @dp.pre_checkout_query()
    async def pre_checkout(pre: PreCheckoutQuery):
        try:
            prefix, uid, stars = pre.invoice_payload.split(":")
            valid = prefix == "wallet_topup" and int(uid) == pre.from_user.id and int(stars) == pre.total_amount and pre.currency == "XTR" and int(stars) in (100, 250, 500, 1000)
        except (ValueError, AttributeError):
            valid = False
        await pre.answer(ok=valid, error_message=None if valid else "بيانات الفاتورة غير صحيحة. أعد طلب الشحن.")

    @dp.message(F.successful_payment)
    async def successful_star_payment(m: Message):
        payment = m.successful_payment
        try:
            prefix, uid, stars = payment.invoice_payload.split(":")
            uid, stars = int(uid), int(stars)
        except (ValueError, AttributeError):
            return await m.answer("تعذر التحقق من بيانات الدفع. تواصل مع الدعم.")
        if prefix != "wallet_topup" or uid != m.from_user.id or payment.currency != "XTR" or payment.total_amount != stars or stars not in (100, 250, 500, 1000):
            return await m.answer("بيانات الدفع غير مطابقة. تواصل مع الدعم.")
        credited = Decimal(stars) / Decimal(100)
        async with pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval("""INSERT INTO telegram_star_payments(telegram_payment_charge_id,telegram_user_id,stars,credited_usd) VALUES($1,$2,$3,$4) ON CONFLICT(telegram_payment_charge_id) DO NOTHING RETURNING telegram_payment_charge_id""", payment.telegram_payment_charge_id, uid, stars, credited)
                if inserted:
                    await conn.execute("INSERT INTO number_wallets(telegram_user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
                    await conn.execute("UPDATE number_wallets SET balance_usd=balance_usd+$2,updated_at=NOW() WHERE telegram_user_id=$1", uid, credited)
                    await conn.execute("INSERT INTO number_transactions(telegram_user_id,amount_usd,kind,note) VALUES($1,$2,'telegram_stars_topup',$3)", uid, credited, f"شحن {stars} نجمة")
                balance = await conn.fetchval("SELECT balance_usd FROM number_wallets WHERE telegram_user_id=$1", uid)
        if inserted:
            await m.answer(f"✅ تم استلام {stars} نجمة وإضافة {yer(credited)} إلى رصيدك.\\nرصيدك الحالي: {yer(balance)}", reply_markup=home_kb())
        else:
            await m.answer("تم تسجيل عملية الدفع هذه سابقاً ولم يُكرر إضافة الرصيد.")

    @dp.callback_query(F.data == "instructions")
    async def instructions(q: CallbackQuery):
        await q.message.answer(
            "📖 طريقة الاستخدام\n"
            "1) افتح شراء رقم واختر الرقم والدولة.\n"
            "2) راجع السعر واضغط تأكيد الشراء.\n"
            "3) بعد مراجعة الطلب وتوفّر الرقم ستصلك تعليمات الاستلام.\n\n"
            "ملاحظة: لا تشارك رموز التحقق أو كلمات المرور مع أي شخص، واستخدم الأرقام فقط في الخدمات والحسابات التي تملكها ويُسمح لك باستخدامها.",
            reply_markup=home_kb()
        )
        await q.answer()

    @dp.callback_query(F.data == "numbers")
    async def numbers(q: CallbackQuery):
        kb = InlineKeyboardBuilder()
        kb.button(text="🟢 أرقام واتساب", callback_data="category:whatsapp")
        kb.button(text="🔵 أرقام تليجرام", callback_data="category:telegram")
        kb.button(text="⬅️ الرئيسية", callback_data="home")
        kb.adjust(1)
        await q.message.answer("📱 اختر نوع الأرقام:", reply_markup=kb.as_markup())
        await q.answer()

    async def show_number_category(q: CallbackQuery, platform: str):
        if platform == "whatsapp":
            condition = "(name ILIKE '%whatsapp%' OR name ILIKE '%واتساب%' OR category ILIKE '%whatsapp%' OR category ILIKE '%واتساب%' OR description ILIKE '%whatsapp%' OR description ILIKE '%واتساب%')"
            heading = "🟢 أرقام واتساب"
        else:
            condition = "(name ILIKE '%telegram%' OR name ILIKE '%تليجرام%' OR name ILIKE '%تلجرام%' OR category ILIKE '%telegram%' OR category ILIKE '%تليجرام%' OR category ILIKE '%تلجرام%' OR description ILIKE '%telegram%' OR description ILIKE '%تليجرام%' OR description ILIKE '%تلجرام%')"
            heading = "🔵 أرقام تليجرام"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id,name,category,description,price_per_1000 FROM provider_services WHERE active=TRUE AND " + condition + " ORDER BY category,name,id LIMIT 60"
            )
        if not rows:
            kb = InlineKeyboardBuilder()
            kb.button(text="↩️ رجوع للأقسام", callback_data="numbers")
            await q.message.answer(f"لا توجد {heading} متاحة حالياً.", reply_markup=kb.as_markup())
            return
        kb = InlineKeyboardBuilder()
        lines = [heading, ""]
        for r in rows:
            price = Decimal(r["price_per_1000"])
            lines.append(f"{r['name']}\\nالدولة: {r['category']} | السعر: {yer(price)}")
            if r["description"]:
                lines.append(str(r["description"])[:160])
            lines.append("")
            kb.button(text=f"شراء {r['name'][:32]} — {yer(price)}", callback_data=f"buy:{r['id']}")
        kb.button(text="↩️ رجوع للأقسام", callback_data="numbers")
        kb.adjust(1)
        await q.message.answer("\\n".join(lines), reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "category:whatsapp")
    async def whatsapp_numbers(q: CallbackQuery):
        await show_number_category(q, "whatsapp")
        await q.answer()

    @dp.callback_query(F.data == "category:telegram")
    async def telegram_numbers(q: CallbackQuery):
        await show_number_category(q, "telegram")
        await q.answer()

    @dp.callback_query(F.data.startswith("buy:"))
    async def buy(q: CallbackQuery):
        try:
            sid = int(q.data.split(":", 1)[1])
        except ValueError:
            return await q.answer("طلب غير صالح", show_alert=True)
        async with pool.acquire() as c:
            service = await c.fetchrow(
                "SELECT id,name,category,description,price_per_1000 FROM provider_services WHERE id=$1 AND active=TRUE",
                sid
            )
        if not service:
            return await q.answer("هذا الرقم لم يعد متاحاً.", show_alert=True)
        price = Decimal(service["price_per_1000"])
        balance = await ensure_wallet(q.from_user.id)
        if price <= 0:
            return await q.answer("السعر غير مضبوط؛ تواصل مع الدعم.", show_alert=True)
        if Decimal(balance) < price:
            kb = InlineKeyboardBuilder()
            kb.button(text="➕ شحن الرصيد", callback_data="topup")
            kb.button(text="⬅️ العودة للأرقام", callback_data="numbers")
            kb.adjust(1)
            await q.message.answer(
                f"رصيدك غير كافٍ.\nالسعر: {yer(price)}\nرصيدك: {yer(balance)}",
                reply_markup=kb.as_markup()
            )
            return await q.answer()
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ تأكيد طلب الرقم", callback_data=f"confirm:{sid}")
        kb.button(text="❌ إلغاء", callback_data="numbers")
        kb.adjust(1)
        await q.message.answer(
            f"تأكيد الطلب\n\nالرقم/الخدمة: {service['name']}\n"
            f"الدولة: {service['category']}\nالسعر: {yer(price)}\n"
            f"رصيدك الحالي: {yer(balance)}\n"
            f"الرصيد بعد الحجز: {yer(Decimal(balance)-price)}\n\n"
            "سيتم حجز المبلغ عند إنشاء الطلب، ويُراجع الطلب قبل التسليم.",
            reply_markup=kb.as_markup()
        )
        await q.answer()

    @dp.callback_query(F.data.startswith("confirm:"))
    async def confirm(q: CallbackQuery):
        try:
            sid = int(q.data.split(":", 1)[1])
        except ValueError:
            return await q.answer("طلب غير صالح", show_alert=True)
        async with pool.acquire() as c:
            async with c.transaction():
                service = await c.fetchrow(
                    "SELECT id,name,price_per_1000 FROM provider_services WHERE id=$1 AND active=TRUE",
                    sid
                )
                if not service:
                    return await q.answer("الرقم لم يعد متاحاً.", show_alert=True)
                price = Decimal(service["price_per_1000"])
                await c.execute("INSERT INTO number_wallets(telegram_user_id) VALUES($1) ON CONFLICT DO NOTHING", q.from_user.id)
                wallet = await c.fetchrow(
                    "SELECT balance_usd FROM number_wallets WHERE telegram_user_id=$1 FOR UPDATE",
                    q.from_user.id
                )
                if Decimal(wallet["balance_usd"]) < price:
                    return await q.answer("رصيدك لم يعد كافياً. اشحن رصيدك أولاً.", show_alert=True)
                await c.execute(
                    "UPDATE number_wallets SET balance_usd=balance_usd-$2,updated_at=NOW() WHERE telegram_user_id=$1",
                    q.from_user.id, price
                )
                order_id = await c.fetchval("""
                    INSERT INTO number_orders(telegram_user_id,service_id,service_name,price_usd,status)
                    VALUES($1,$2,$3,$4,'pending_review') RETURNING id
                """, q.from_user.id, sid, service["name"], price)
                await c.execute("""
                    INSERT INTO number_transactions(telegram_user_id,amount_usd,kind,note)
                    VALUES($1,$2,'purchase_hold',$3)
                """, q.from_user.id, -price, f"حجز طلب رقم #{order_id}")
        await q.message.answer(
            f"✅ تم تسجيل طلبك رقم #{order_id}.\n"
            f"الرقم المطلوب: {service['name']}\nالسعر المحجوز: {yer(price)}\n"
            "الحالة: قيد مراجعة الإدارة. ستصلك رسالة عند تحديث الحالة.",
            reply_markup=home_kb()
        )
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"طلب رقم جديد #{order_id}\nالعميل: {q.from_user.full_name} ({q.from_user.id})\nالرقم: {service['name']}\nالسعر: {yer(price)}")
            except Exception:
                pass
        await q.answer("تم تسجيل طلبك")

    @dp.callback_query(F.data == "myorders")
    async def myorders(q: CallbackQuery):
        async with pool.acquire() as c:
            rows = await c.fetch("""
                SELECT id,service_name,price_usd,status,created_at
                FROM number_orders WHERE telegram_user_id=$1 ORDER BY id DESC LIMIT 15
            """, q.from_user.id)
        if not rows:
            msg = "لا توجد طلبات أرقام مسجلة لك."
        else:
            msg = "📦 طلباتك\n\n" + "\n\n".join(
                f"#{r['id']} — {r['service_name']}\nالسعر: {yer(r['price_usd'])}\nالحالة: {r['status']}"
                for r in rows
            )
        await q.message.answer(msg, reply_markup=home_kb())
        await q.answer()

    @dp.callback_query(F.data == "support")
    async def support(q: CallbackQuery, state: FSMContext):
        await state.set_state(SupportFlow.message)
        await q.message.answer("أرسل رسالتك للدعم الفني:")
        await q.answer()

    @dp.message(SupportFlow.message)
    async def support_message(m: Message, state: FSMContext):
        body = (m.text or "").strip()
        if not body:
            return await m.answer("أرسل رسالة نصية من فضلك.")
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"رسالة دعم من {m.from_user.full_name} ({m.from_user.id}):\n{body[:3000]}")
            except Exception:
                pass
        await state.clear()
        await m.answer("تم إرسال رسالتك للدعم.", reply_markup=home_kb())

    @dp.message(Command("credit"))
    async def credit(m: Message):
        if m.from_user.id not in ADMIN_IDS:
            return
        parts = (m.text or "").split()
        if len(parts) != 3:
            return await m.answer("الاستخدام: /credit USER_ID AMOUNT_USD")
        try:
            uid = int(parts[1])
            amount = Decimal(parts[2])
            if amount <= 0 or amount > Decimal("100000"):
                raise ValueError()
        except (ValueError, InvalidOperation):
            return await m.answer("أدخل آيدي صحيحاً ومبلغاً موجباً.")
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute("INSERT INTO number_wallets(telegram_user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
                await c.execute("UPDATE number_wallets SET balance_usd=balance_usd+$2,updated_at=NOW() WHERE telegram_user_id=$1", uid, amount)
                await c.execute("INSERT INTO number_transactions(telegram_user_id,amount_usd,kind,note) VALUES($1,$2,'admin_credit',$3)", uid, amount, f"شحن يدوي بواسطة المشرف {m.from_user.id}")
        await m.answer(f"تم إضافة {yer(amount)} إلى رصيد {uid}.")
        try:
            await bot.send_message(uid, f"تم شحن رصيدك بمبلغ {yer(amount)}.")
        except Exception:
            pass

    try:
        await dp.start_polling(bot)
    finally:
        if pool:
            await pool.close()

if __name__ == "__main__":
    asyncio.run(main())

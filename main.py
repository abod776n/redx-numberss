"""RedX Numbers — shared PostgreSQL provider manager.
Provider credentials are encrypted; this bot only manages generic service-provider APIs.
"""
import asyncio, ipaddress, json, os, socket
from urllib.parse import urlparse
import aiohttp, asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cryptography.fernet import Fernet

BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
ADMIN_IDS={int(x) for x in os.getenv("ADMIN_IDS","").replace(" ","").split(",") if x}
FERNET_KEY=os.getenv("CONFIG_ENCRYPTION_KEY","").strip()
DATABASE_URL=os.getenv("DATABASE_URL","").strip()
if not BOT_TOKEN or not ADMIN_IDS or not FERNET_KEY or not DATABASE_URL:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN, ADMIN_IDS, CONFIG_ENCRYPTION_KEY and DATABASE_URL")
fernet=Fernet(FERNET_KEY.encode())
pool=None

class AddProvider(StatesGroup):
    name=State(); url=State(); auth_mode=State(); api_key=State(); services_path=State(); balance_path=State()

def is_admin(uid): return uid in ADMIN_IDS

def safe_base_url(value):
    p=urlparse(value.strip())
    if p.scheme not in {"https","http"} or not p.hostname or p.username or p.password:
        raise ValueError("أرسل رابط HTTPS صحيحاً، أو HTTP للمزوّد 38.60.250.36 فقط.")
    host=p.hostname.lower()
    if host in {"localhost","localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("العنوان المحلي غير مسموح.")
    if p.scheme=="http" and host!="38.60.250.36":
        raise ValueError("اتصال HTTP مسموح فقط للمزوّد 38.60.250.36. استخدم HTTPS لبقية المزودين.")
    try:
        ip=ipaddress.ip_address(host)
        if not ip.is_global: raise ValueError("عناوين الشبكات الداخلية غير مسموحة.")
    except ValueError as e:
        if "غير مسموح" in str(e): raise
        try: infos=socket.getaddrinfo(host,p.port or (443 if p.scheme=="https" else 80),type=socket.SOCK_STREAM)
        except OSError: raise ValueError("تعذر التحقق من عنوان الموقع.")
        for info in infos:
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise ValueError("الموقع يحيل إلى عنوان شبكة داخلية وغير مسموح.")
    return value.strip().rstrip("/")

async def api_get(base_url,path,mode,secret):
    base_url=safe_base_url(base_url)
    if not path.startswith("/") or path.startswith("//"): raise ValueError("مسار API غير صالح.")
    headers={"Accept":"application/json"}
    if mode=="bearer": headers["Authorization"]=f"Bearer {secret}"
    elif mode=="x-api-key": headers["X-API-Key"]=secret
    elif mode.startswith("custom:"): headers[mode.split(":",1)[1]]=secret
    timeout=aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(base_url+path,headers=headers,allow_redirects=False) as r:
            body=await r.text()
            if 300<=r.status<400: raise ValueError("تم إيقاف تحويل API للحماية.")
            try: data=json.loads(body)
            except json.JSONDecodeError: data=body[:1200]
            if r.status>=400: raise ValueError(f"HTTP {r.status}: {str(data)[:500]}")
            return r.status,data

def extract_services(data):
    # ReadyWA API schema: {ok:true,data:{count:N,services:[...]}}
    if isinstance(data,dict):
        payload=data.get("data",data)
        if isinstance(payload,dict) and isinstance(payload.get("services"),list):
            data=payload["services"]
        elif isinstance(payload,dict):
            for key in ("services","results","items"):
                if isinstance(payload.get(key),list):
                    data=payload[key]; break
        elif isinstance(payload,list):
            data=payload
    if not isinstance(data,list):
        raise ValueError("رد المزود لا يحتوي data.services؛ تحقق من المفتاح ومسار /services.")
    out=[]
    for item in data:
        if not isinstance(item,dict): continue
        sid=item.get("service_id",item.get("service",item.get("id")))
        name=item.get("name",item.get("title"))
        if sid is None or not name: continue
        try: price=float(item.get("price_usd",item.get("rate",item.get("price",0))) or 0)
        except (TypeError,ValueError): price=0
        section=str(item.get("section") or item.get("category") or item.get("platform") or item.get("app") or item.get("product_type") or item.get("service_type") or "عام")[:80]
        stock=item.get("stock")
        available=item.get("available",True)
        tags=" ".join(str(item.get(k,"")) for k in ("platform","app","product_type","service_type","type","country","country_name","description") if item.get(k) is not None)
        description=f"السعر للطلب الواحد: {price:.4f} USD | المخزون: {stock if stock is not None else 'غير محدد'} | متاح: {'نعم' if available else 'لا'} | بيانات التصنيف: {tags[:350]}"
        # Each imported row is one number/service; provider purchase and OTP endpoints are not called here.
        out.append((str(sid),str(name)[:180],section,description,price,1,1))
    if not out:
        raise ValueError("تم استلام الرد، لكن لم أجد خدمات تحتوي service_id و name. تأكد من صلاحية المفتاح.")
    return out

def home_kb():
    kb=InlineKeyboardBuilder()
    kb.button(text="➕ إضافة مزوّد API",callback_data="add")
    kb.button(text="📋 المزوّدون والخدمات",callback_data="list")
    kb.adjust(1)
    return kb.as_markup()

async def main():
    global pool
    pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=5,command_timeout=30)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS providers(
          id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,base_url TEXT NOT NULL,
          auth_mode TEXT NOT NULL,secret BYTEA NOT NULL,services_path TEXT NOT NULL DEFAULT '',
          balance_path TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ DEFAULT NOW())
        """)
        await c.execute("""
        CREATE TABLE IF NOT EXISTS provider_services(
          id BIGSERIAL PRIMARY KEY,provider_id BIGINT REFERENCES providers(id) ON DELETE CASCADE,
          external_service_id TEXT,name TEXT NOT NULL,category TEXT NOT NULL DEFAULT 'عام',
          description TEXT NOT NULL DEFAULT '',price_per_1000 NUMERIC(18,6) NOT NULL DEFAULT 0,
          min_quantity BIGINT NOT NULL DEFAULT 1,max_quantity BIGINT NOT NULL DEFAULT 1000000,
          active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(provider_id,external_service_id))
        """)
        await c.execute("""
        CREATE TABLE IF NOT EXISTS customer_orders(
          id BIGSERIAL PRIMARY KEY,telegram_user_id BIGINT NOT NULL,telegram_username TEXT,
          service_id BIGINT NOT NULL REFERENCES provider_services(id),target_url TEXT NOT NULL,
          quantity BIGINT NOT NULL,quoted_price NUMERIC(18,6) NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending_review',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())
        """)
    bot=Bot(BOT_TOKEN); dp=Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(m:Message):
        if not is_admin(m.from_user.id): return await m.answer("غير مصرح لك باستخدام لوحة الإدارة.")
        await m.answer("🔌 **RedX Numbers | إدارة المزوّدين**\nأضف مزوّداً ثم استورد خدماته إلى كتالوج طلباتي نمبر.",reply_markup=home_kb(),parse_mode="Markdown")

    @dp.callback_query(F.data=="home")
    async def home(q:CallbackQuery):
        if is_admin(q.from_user.id): await q.message.edit_text("لوحة إدارة المزوّدين",reply_markup=home_kb())
        await q.answer()

    @dp.callback_query(F.data=="add")
    async def add(q:CallbackQuery,state:FSMContext):
        if not is_admin(q.from_user.id): return await q.answer("غير مصرح",show_alert=True)
        await state.clear(); await state.set_state(AddProvider.name)
        await q.message.answer("أرسل اسم المزوّد:"); await q.answer()

    @dp.message(AddProvider.name)
    async def name(m:Message,state:FSMContext):
        if not is_admin(m.from_user.id): return
        await state.update_data(name=(m.text or "").strip()[:80])
        await state.set_state(AddProvider.url)
        await m.answer("أرسل رابط API الأساسي (HTTPS)، مثال: https://api.example.com/v1")

    @dp.message(AddProvider.url)
    async def url(m:Message,state:FSMContext):
        if not is_admin(m.from_user.id): return
        try: value=safe_base_url(m.text or "")
        except Exception as e: return await m.answer(f"❌ {e}\nأعد إرسال الرابط.")
        await state.update_data(url=value); await state.set_state(AddProvider.auth_mode)
        kb=InlineKeyboardBuilder()
        for label,mode in [("Bearer Token","bearer"),("X-API-Key","x-api-key"),("Custom Header","custom")]:
            kb.button(text=label,callback_data="auth:"+mode)
        kb.adjust(1); await m.answer("اختر طريقة المصادقة:",reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("auth:"))
    async def auth(q:CallbackQuery,state:FSMContext):
        if not is_admin(q.from_user.id): return await q.answer("غير مصرح",show_alert=True)
        mode=q.data.split(":",1)[1]
        if mode=="custom":
            await state.update_data(wait_custom=True); await state.set_state(AddProvider.auth_mode)
            await q.message.answer("أرسل اسم ترويسة المصادقة، مثال X-API-Key")
        else:
            await state.update_data(auth_mode=mode); await state.set_state(AddProvider.api_key)
            await q.message.answer("أرسل مفتاح API. سيتم حذف رسالتك بعد استلامه.")
        await q.answer()

    @dp.message(AddProvider.auth_mode)
    async def custom(m:Message,state:FSMContext):
        if not is_admin(m.from_user.id): return
        d=await state.get_data()
        if not d.get("wait_custom"): return
        h=(m.text or "").strip()
        if not h or len(h)>80 or any(x in h for x in "\r\n:"): return await m.answer("اسم الترويسة غير صالح.")
        await state.update_data(auth_mode="custom:"+h,wait_custom=False); await state.set_state(AddProvider.api_key)
        await m.answer("أرسل مفتاح API. سيتم حذف رسالتك بعد استلامه.")

    @dp.message(AddProvider.api_key)
    async def key(m:Message,state:FSMContext):
        if not is_admin(m.from_user.id): return
        secret=(m.text or "").strip()
        if len(secret)<4 or len(secret)>500: return await m.answer("المفتاح غير صالح.")
        await state.update_data(secret=secret)
        try: await m.delete()
        except Exception: pass
        await state.set_state(AddProvider.services_path)
        await m.answer("أرسل مسار جلب الخدمات مثل /services")

    @dp.message(AddProvider.services_path)
    async def spath(m:Message,state:FSMContext):
        if not is_admin(m.from_user.id): return
        path=(m.text or "").strip()
        if not path.startswith("/") or path.startswith("//"): return await m.answer("أرسل مساراً يبدأ بـ / مثل /services")
        await state.update_data(services_path=path); await state.set_state(AddProvider.balance_path)
        await m.answer("أرسل مسار الرصيد مثل /balance أو - للتخطي:")

    @dp.message(AddProvider.balance_path)
    async def bpath(m:Message,state:FSMContext):
        if not is_admin(m.from_user.id): return
        path=(m.text or "").strip(); path="" if path=="-" else path
        if path and (not path.startswith("/") or path.startswith("//")): return await m.answer("المسار غير صالح، أرسل /balance أو -")
        d=await state.get_data()
        async with pool.acquire() as c:
            pid=await c.fetchval("""INSERT INTO providers(name,base_url,auth_mode,secret,services_path,balance_path)
              VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
              d["name"],d["url"],d["auth_mode"],fernet.encrypt(d["secret"].encode()),d["services_path"],path)
        await state.clear()
        await m.answer(f"✅ تم حفظ المزوّد رقم #{pid}.\nاضغط «المزوّدون والخدمات» ثم «استيراد الخدمات» لنشر خدماته في البوت الرسمي.",reply_markup=home_kb())

    @dp.callback_query(F.data=="list")
    async def listing(q:CallbackQuery):
        if not is_admin(q.from_user.id): return await q.answer("غير مصرح",show_alert=True)
        async with pool.acquire() as c:
            rows=await c.fetch("""SELECT p.id,p.name,p.base_url,p.services_path,
              COUNT(s.id) FILTER(WHERE s.active) AS active_count
              FROM providers p LEFT JOIN provider_services s ON s.provider_id=p.id
              GROUP BY p.id ORDER BY p.id DESC""")
        if not rows:
            await q.message.edit_text("لا يوجد مزوّدون محفوظون.",reply_markup=home_kb()); return await q.answer()
        kb=InlineKeyboardBuilder(); lines=["📋 **المزوّدون والخدمات المشتركة**"]
        for r in rows:
            lines.append(f"\n**#{r['id']} {r['name']}**\nالخدمات المنشورة: {r['active_count']}\nمسار الخدمات: {r['services_path']}")
            kb.button(text=f"🔄 استيراد خدمات {r['name']}",callback_data=f"sync:{r['id']}")
        kb.button(text="⬅️ الرئيسية",callback_data="home"); kb.adjust(1)
        await q.message.edit_text("\n".join(lines),reply_markup=kb.as_markup(),parse_mode="Markdown"); await q.answer()

    @dp.callback_query(F.data.startswith("sync:"))
    async def sync(q:CallbackQuery):
        if not is_admin(q.from_user.id): return await q.answer("غير مصرح",show_alert=True)
        pid=int(q.data.split(":",1)[1])
        async with pool.acquire() as c:
            row=await c.fetchrow("SELECT * FROM providers WHERE id=$1",pid)
        if not row: return await q.answer("المزوّد غير موجود",show_alert=True)
        if not row["services_path"]: return await q.answer("لم يتم ضبط مسار الخدمات لهذا المزوّد.",show_alert=True)
        await q.answer("جارٍ جلب الخدمات…")
        try:
            secret=fernet.decrypt(bytes(row["secret"])).decode()
            status,data=await api_get(row["base_url"],row["services_path"],row["auth_mode"],secret)
            services=extract_services(data)
            async with pool.acquire() as c:
                async with c.transaction():
                    for sid,n,cat,desc,price,mn,mx in services:
                        await c.execute("""INSERT INTO provider_services
                          (provider_id,external_service_id,name,category,description,price_per_1000,min_quantity,max_quantity,active)
                          VALUES($1,$2,$3,$4,$5,$6,$7,$8,TRUE)
                          ON CONFLICT(provider_id,external_service_id) DO UPDATE SET
                          name=EXCLUDED.name,category=EXCLUDED.category,description=EXCLUDED.description,
                          price_per_1000=EXCLUDED.price_per_1000,min_quantity=EXCLUDED.min_quantity,
                          max_quantity=EXCLUDED.max_quantity,active=TRUE""",
                          pid,sid,n,cat,desc,price,mn,mx)
            await q.message.answer(f"✅ اكتمل استيراد {len(services)} خدمة من {row['name']} إلى قاعدة البيانات المشتركة.\nستظهر الخدمات النشطة في بوت طلباتي نمبر.")
        except Exception as e:
            await q.message.answer(f"❌ تعذر استيراد الخدمات: {str(e)[:700]}")

    await dp.start_polling(bot)

if __name__=="__main__":
    try: asyncio.run(main())
    finally:
        if pool: asyncio.run(pool.close())

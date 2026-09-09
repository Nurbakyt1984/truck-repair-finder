from __future__ import annotations
import logging, math, os, re, tempfile
from pathlib import Path
from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from db import add_service, approve_service, delete_service, get_approved_services, get_pending_services, get_user_services, init_db

load_dotenv(); TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); SEARCH_RADIUS_MILES=float(os.getenv("SEARCH_RADIUS_MILES","100"))
ADMIN_IDS={int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO); logger=logging.getLogger("truck-repair-finder")
CHOOSE, INPUT, LOCATION, CONFIRM = range(4)
MAIN_KB=ReplyKeyboardMarkup([["📍 Найти рядом","➕ Добавить сервис"],["📋 Мои заявки","ℹ️ Помощь"]],resize_keyboard=True)
ADD_KB=ReplyKeyboardMarkup([["📷 Фото / скриншот"],[KeyboardButton("📇 Отправить контакт",request_contact=True)],["✍️ Ввести вручную"],["❌ Отмена"]],resize_keyboard=True)
LOCATION_KB=ReplyKeyboardMarkup([[KeyboardButton("📍 Отправить геолокацию",request_location=True)],["❌ Отмена"]],resize_keyboard=True)
CONFIRM_KB=ReplyKeyboardMarkup([["✅ Сохранить","✏️ Изменить"],["❌ Отмена"]],resize_keyboard=True)

def miles_between(a,b,c,d):
    r=3958.7613;p1,p2=math.radians(a),math.radians(c);dp=math.radians(c-a);dl=math.radians(d-b);x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2;return 2*r*math.asin(math.sqrt(x))
def maps_link(lat,lon): return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
def val(o,n,default=""): return getattr(o,n,default) or default

def card(d, distance=None):
    out=[f"🔧 {d.get('name') or 'Без названия'}",f"🏷 {d.get('category') or 'Truck Repair'}"]
    if d.get('rating') is not None:
        reviews=f" · {d.get('review_count')} отзывов" if d.get('review_count') is not None else ""; out.append(f"⭐ {d['rating']:.1f}/5{reviews}")
    if distance is not None: out.append(f"📏 {distance:.1f} mi")
    if d.get('phone'): out.append(f"📞 {d['phone']}")
    if d.get('address'): out.append(f"📍 {d['address']}")
    if d.get('languages'): out.append(f"🌐 Языки: {d['languages']}")
    if d.get('hours'): out.append(f"🕐 {d['hours']}")
    if d.get('website'): out.append(f"🌐 {d['website']}")
    if d.get('notes'): out.append(f"📝 {d['notes']}")
    if d.get('latitude') is not None: out.append(f"🗺 Маршрут: {maps_link(d['latitude'],d['longitude'])}")
    return "\n".join(out)
def item_dict(i): return {k:val(i,k) for k in ['name','category','phone','address','languages','hours','website','notes']}|{'rating':val(i,'rating',None),'review_count':val(i,'review_count',None),'latitude':i.latitude,'longitude':i.longitude}

def parse_text(t):
    lines=[x.strip() for x in t.splitlines() if x.strip()]; d={'category':'Truck Repair','phone':'','address':'','languages':'','rating':None,'review_count':None,'hours':'','website':'','notes':''}
    keys={'название':'name','name':'name','категория':'category','category':'category','телефон':'phone','phone':'phone','адрес':'address','address':'address','языки':'languages','languages':'languages','рейтинг':'rating','rating':'rating','отзывы':'review_count','reviews':'review_count','часы':'hours','hours':'hours','сайт':'website','website':'website','примечание':'notes','notes':'notes'}
    for line in lines:
        if ':' in line:
            k,v=line.split(':',1); key=keys.get(k.strip().lower().replace('⭐','').replace('🌐','').strip())
            if key:
                v=v.strip()
                try:
                    if key=='rating': d[key]=float(re.search(r'[0-5](?:[.,]\d+)?',v).group().replace(',','.'))
                    elif key=='review_count': d[key]=int(re.sub(r'\D','',v))
                    else: d[key]=v
                except Exception: pass
    full=' '.join(lines)
    if not d.get('phone'):
        m=re.search(r'(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}',full); d['phone']=m.group(0) if m else ''
    if not d.get('rating'):
        m=re.search(r'\b([1-4][.,]\d|5[.,]0)\b',full); d['rating']=float(m.group(1).replace(',','.')) if m else None
    if not d.get('website'):
        m=re.search(r'(https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|net|org|us)\b)',full,re.I); d['website']=m.group(0) if m else ''
    if not d.get('name') and lines: d['name']=lines[0][:150]
    return d

async def start(u,c): await u.message.reply_text("👋 Добро пожаловать в Поиск Автосервисов!\n\n📍 Найти рядом — сервисы в радиусе 100 миль\n➕ Добавить сервис — предложить сервис на проверку\n📋 Мои заявки — статус ваших предложений\nℹ️ Помощь — справка",reply_markup=MAIN_KB)
async def help_cmd(u,c): await u.message.reply_text("ℹ️ Найдите сервис рядом или добавьте новый. При добавлении можно отправить фото визитки, скриншот Google Maps, Telegram-контакт или заполнить одну форму вручную.",reply_markup=MAIN_KB)
async def ask_location(u,c): await u.message.reply_text("📍 Нажмите кнопку ниже, чтобы поделиться геолокацией:",reply_markup=LOCATION_KB)
async def search_location(u,c):
    loc=u.message.location; items=[]
    for i in get_approved_services():
        dist=miles_between(loc.latitude,loc.longitude,i.latitude,i.longitude)
        if dist<=SEARCH_RADIUS_MILES: items.append((dist,i))
    items.sort(key=lambda x:(x[0],-(x[1].rating or 0)))
    if not items: await u.message.reply_text(f"В радиусе {SEARCH_RADIUS_MILES:g} миль пока ничего не найдено.",reply_markup=MAIN_KB); return
    await u.message.reply_text(f"🔎 Найдено сервисов: {len(items)}",reply_markup=MAIN_KB)
    for dist,i in items[:10]: await u.message.reply_text(card(item_dict(i),dist),disable_web_page_preview=True)
async def my_submissions(u,c):
    items=get_user_services(u.effective_user.id)
    if not items: await u.message.reply_text("У вас пока нет заявок.",reply_markup=MAIN_KB); return
    await u.message.reply_text("📋 Ваши заявки:\n\n"+'\n'.join(f"#{i.id} — {i.name} — {'✅ Одобрено' if i.approved else '⏳ На проверке'}" for i in items[:20]),reply_markup=MAIN_KB)

async def add_start(u,c): c.user_data['new_service']={}; await u.message.reply_text("➕ Как хотите добавить сервис?",reply_markup=ADD_KB); return CHOOSE
async def choose(u,c):
    if u.message.contact:
        ct=u.message.contact;c.user_data['new_service']={'name':(' '.join(x for x in [ct.first_name,ct.last_name] if x)).strip(),'phone':ct.phone_number,'category':'Truck Repair','address':'','languages':'','rating':None,'review_count':None,'hours':'','website':'','notes':''}; await u.message.reply_text("📍 Теперь отправьте геолокацию сервиса.",reply_markup=LOCATION_KB); return LOCATION
    t=u.message.text
    if t=='📷 Фото / скриншот': await u.message.reply_text("📷 Отправьте фото визитки или скриншот Google Maps."); return INPUT
    if t=='✍️ Ввести вручную':
        template="""📝 Скопируйте форму ниже, заполните известные поля и отправьте одним сообщением:\n\nНазвание:\nКатегория: Truck Repair\nТелефон:\nАдрес:\nЯзыки: English, Русский\nРейтинг:\nОтзывы:\nЧасы:\nСайт:\nПримечание:"""; await u.message.reply_text(template); return INPUT
    return CHOOSE
async def photo_or_text(u,c):
    if u.message.photo:
        try:
            from rapidocr_onnxruntime import RapidOCR
            f=await u.message.photo[-1].get_file()
            with tempfile.NamedTemporaryFile(suffix='.jpg',delete=False) as tmp: path=tmp.name
            await f.download_to_drive(path); result,_=RapidOCR()(path); Path(path).unlink(missing_ok=True)
            raw='\n'.join(x[1] for x in (result or [])); d=parse_text(raw)
            await u.message.reply_text("📷 Я распознал данные. Проверьте их ниже.")
        except Exception:
            logger.exception('OCR failed'); await u.message.reply_text("Не удалось распознать фото автоматически. Отправьте данные одной формой текстом."); return INPUT
    else: d=parse_text(u.message.text)
    c.user_data['new_service']=d; await u.message.reply_text(card(d)+"\n\n📍 Теперь отправьте геолокацию сервиса.",reply_markup=LOCATION_KB); return LOCATION
async def add_location(u,c):
    d=c.user_data['new_service'];d['latitude']=u.message.location.latitude;d['longitude']=u.message.location.longitude
    await u.message.reply_text("🪪 Проверьте карточку:\n\n"+card(d),reply_markup=CONFIRM_KB,disable_web_page_preview=True);return CONFIRM
async def confirm(u,c):
    if u.message.text=='✏️ Изменить':
        await u.message.reply_text("✏️ Отправьте всю исправленную форму одним сообщением. После этого снова отправьте геолокацию.");return INPUT
    if u.message.text!='✅ Сохранить': return CONFIRM
    d=c.user_data['new_service']; item=add_service(**d,submitted_by=u.effective_user.id,approved=u.effective_user.id in ADMIN_IDS)
    await u.message.reply_text(f"Готово. Сервис #{item.id} {'сразу одобрен ✅' if item.approved else 'отправлен на проверку ⏳'}.",reply_markup=MAIN_KB)
    if not item.approved:
        for aid in ADMIN_IDS:
            try: await c.bot.send_message(aid,"🆕 Новая заявка\n\n"+card(item_dict(item))+f"\n\nОдобрить: /approve {item.id}\nУдалить: /delete {item.id}",disable_web_page_preview=True)
            except Exception: logger.exception('admin notify')
    c.user_data.pop('new_service',None);return ConversationHandler.END
async def cancel(u,c): c.user_data.pop('new_service',None);await u.message.reply_text("Отменено.",reply_markup=MAIN_KB);return ConversationHandler.END

def is_admin(x): return x is not None and x in ADMIN_IDS
async def pending(u,c):
    if not is_admin(u.effective_user.id): return
    for i in get_pending_services()[:20]: await u.message.reply_text(card(item_dict(i))+f"\n\nОдобрить: /approve {i.id}\nУдалить: /delete {i.id}",disable_web_page_preview=True)
async def approve(u,c):
    if is_admin(u.effective_user.id) and c.args and c.args[0].isdigit(): await u.message.reply_text("✅ Одобрено." if approve_service(int(c.args[0])) else "Сервис не найден.")
async def delete(u,c):
    if is_admin(u.effective_user.id) and c.args and c.args[0].isdigit(): await u.message.reply_text("🗑 Удалено." if delete_service(int(c.args[0])) else "Сервис не найден.")
async def error_handler(u,c): logger.exception("Unhandled exception",exc_info=c.error)

def build_app():
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    init_db();app=Application.builder().token(TOKEN).build()
    conv=ConversationHandler(entry_points=[MessageHandler(filters.Regex(r'^➕ Добавить сервис$'),add_start),CommandHandler('add',add_start)],states={CHOOSE:[MessageHandler(filters.CONTACT,choose),MessageHandler(filters.TEXT & ~filters.COMMAND,choose)],INPUT:[MessageHandler(filters.PHOTO,photo_or_text),MessageHandler(filters.TEXT & ~filters.COMMAND,photo_or_text)],LOCATION:[MessageHandler(filters.LOCATION,add_location)],CONFIRM:[MessageHandler(filters.TEXT & ~filters.COMMAND,confirm)]},fallbacks=[CommandHandler('cancel',cancel),MessageHandler(filters.Regex(r'^❌ Отмена$'),cancel)])
    app.add_handler(CommandHandler('start',start));app.add_handler(CommandHandler('help',help_cmd));app.add_handler(CommandHandler('pending',pending));app.add_handler(CommandHandler('approve',approve));app.add_handler(CommandHandler('delete',delete));app.add_handler(conv);app.add_handler(MessageHandler(filters.Regex(r'^📍 Найти рядом$'),ask_location));app.add_handler(MessageHandler(filters.LOCATION,search_location));app.add_handler(MessageHandler(filters.Regex(r'^📋 Мои заявки$'),my_submissions));app.add_handler(MessageHandler(filters.Regex(r'^ℹ️ Помощь$'),help_cmd));app.add_error_handler(error_handler);return app
if __name__=='__main__': build_app().run_polling(allowed_updates=Update.ALL_TYPES)

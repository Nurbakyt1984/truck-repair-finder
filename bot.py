from __future__ import annotations
import base64, json, logging, math, os, re, tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from db import add_service, approve_service, delete_service, get_approved_services, get_pending_services, get_user_services, init_db

load_dotenv()
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip(); SEARCH_RADIUS_MILES=float(os.getenv('SEARCH_RADIUS_MILES','100'))
GOOGLE_VISION_API_KEY=os.getenv('GOOGLE_VISION_API_KEY','').strip()
ADMIN_IDS={int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
logging.basicConfig(format='%(asctime)s %(levelname)s %(name)s: %(message)s',level=logging.INFO); logger=logging.getLogger('truck-repair-finder')
CHOOSE, INPUT, CONFIRM = range(3)
MAIN_KB=ReplyKeyboardMarkup([['📍 Найти рядом','➕ Добавить сервис'],['📋 Мои заявки','ℹ️ Помощь']],resize_keyboard=True)
ADD_KB=ReplyKeyboardMarkup([['📷 Фото / скриншот'],[KeyboardButton('📇 Отправить контакт',request_contact=True)],['✍️ Ввести вручную'],['❌ Отмена']],resize_keyboard=True)
SEARCH_LOCATION_KB=ReplyKeyboardMarkup([[KeyboardButton('📍 Отправить мою геолокацию',request_location=True)],['❌ Отмена']],resize_keyboard=True,one_time_keyboard=True)
CONFIRM_KB=ReplyKeyboardMarkup([['✅ Сохранить','✏️ Изменить'],['❌ Отмена']],resize_keyboard=True)
FORM='''📝 Заполните известные поля и отправьте всё одним сообщением:\n\nНазвание:\nКатегория: Truck Repair\nТелефон:\nАдрес:\nЯзыки:\nРейтинг:\nОтзывы:\nЧасы:\nСайт:\nПримечание:\n\n📍 Геолокацию сервиса отправлять не нужно — координаты определю по адресу.'''

def miles_between(a,b,c,d):
    r=3958.7613;p1,p2=math.radians(a),math.radians(c);dp=math.radians(c-a);dl=math.radians(d-b);x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2;return 2*r*math.asin(math.sqrt(x))
def maps_link(lat,lon): return f'https://www.google.com/maps/dir/?api=1&destination={lat},{lon}'
def val(o,n,default=''): return getattr(o,n,default) or default

def card(d,distance=None):
    out=[f"🔧 {d.get('name') or 'Без названия'}",f"🏷 {d.get('category') or 'Truck Repair'}"]
    if d.get('rating') is not None:
        reviews=f" · {d.get('review_count')} отзывов" if d.get('review_count') is not None else ''; out.append(f"⭐ {d['rating']:.1f}/5{reviews}")
    if distance is not None: out.append(f'📏 {distance:.1f} mi')
    if d.get('phone'): out.append(f"📞 {d['phone']}")
    if d.get('address'): out.append(f"📍 {d['address']}")
    if d.get('languages'): out.append(f"🌐 Языки: {d['languages']}")
    if d.get('hours'): out.append(f"🕐 {d['hours']}")
    if d.get('website'): out.append(f"🌐 {d['website']}")
    if d.get('notes'): out.append(f"📝 {d['notes']}")
    if d.get('latitude') is not None: out.append(f"🗺 Маршрут: {maps_link(d['latitude'],d['longitude'])}")
    return '\n'.join(out)
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
                    if key=='rating' and v: d[key]=float(re.search(r'[0-5](?:[.,]\d+)?',v).group().replace(',','.'))
                    elif key=='review_count' and v: d[key]=int(re.sub(r'\D','',v))
                    else: d[key]=v
                except Exception: pass
    full=' '.join(lines)
    if not d.get('phone'):
        m=re.search(r'(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}',full); d['phone']=m.group(0) if m else ''
    if not d.get('website'):
        m=re.search(r'(https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|net|org|us)\b)',full,re.I); d['website']=m.group(0) if m else ''
    if not d.get('name') and lines: d['name']=lines[0][:150]
    if not d.get('languages') and re.search(r'se habla espa[nñ]ol|espa[nñ]ol|spanish',full,re.I): d['languages']='Español'
    if not d.get('hours') and re.search(r'24\s*hours?|24\s*/\s*7',full,re.I): d['hours']='24/7'
    return d

def google_vision_ocr(path: str) -> str:
    if not GOOGLE_VISION_API_KEY:
        raise RuntimeError('GOOGLE_VISION_API_KEY is missing')
    content=base64.b64encode(Path(path).read_bytes()).decode('ascii')
    payload={
        'requests': [{
            'image': {'content': content},
            'features': [{'type': 'TEXT_DETECTION', 'maxResults': 1}],
        }]
    }
    url='https://vision.googleapis.com/v1/images:annotate?'+urlencode({'key':GOOGLE_VISION_API_KEY})
    req=Request(url,data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'},method='POST')
    with urlopen(req,timeout=30) as r:
        data=json.loads(r.read().decode('utf-8'))
    response=(data.get('responses') or [{}])[0]
    if response.get('error'):
        raise RuntimeError(response['error'].get('message') or str(response['error']))
    annotations=response.get('textAnnotations') or []
    return annotations[0].get('description','').strip() if annotations else ''

def geocode_address(address):
    if not address.strip(): return None
    params=urlencode({'q':address,'format':'jsonv2','limit':1,'countrycodes':'us'})
    req=Request('https://nominatim.openstreetmap.org/search?'+params,headers={'User-Agent':'TruckRepairFinderBot/1.0'})
    with urlopen(req,timeout=12) as r: data=json.loads(r.read().decode('utf-8'))
    if not data: return None
    return float(data[0]['lat']),float(data[0]['lon']),data[0].get('display_name','')

async def start(u,c): await u.message.reply_text(f'👋 Добро пожаловать в Поиск Автосервисов!\n\n📍 Найти рядом — сервисы в радиусе {SEARCH_RADIUS_MILES:g} миль\n➕ Добавить сервис — предложить сервис на проверку\n📋 Мои заявки — статус ваших предложений\nℹ️ Помощь — справка',reply_markup=MAIN_KB)
async def help_cmd(u,c): await u.message.reply_text('ℹ️ Можно добавить сервис фото/скриншотом, контактом или одной ручной формой. Для сервиса достаточно адреса — координаты бот определит сам.',reply_markup=MAIN_KB)
async def ask_location(u,c): await u.message.reply_text('📍 Отправьте вашу геолокацию, чтобы найти сервисы рядом:',reply_markup=SEARCH_LOCATION_KB)
async def search_location(u,c):
    loc=u.message.location; items=[]
    for i in get_approved_services():
        dist=miles_between(loc.latitude,loc.longitude,i.latitude,i.longitude)
        if dist<=SEARCH_RADIUS_MILES: items.append((dist,i))
    items.sort(key=lambda x:(x[0],-(x[1].rating or 0)))
    if not items: await u.message.reply_text(f'В радиусе {SEARCH_RADIUS_MILES:g} миль пока ничего не найдено.',reply_markup=MAIN_KB); return
    await u.message.reply_text(f'🔎 Найдено сервисов: {len(items)}',reply_markup=MAIN_KB)
    for dist,i in items[:10]: await u.message.reply_text(card(item_dict(i),dist),disable_web_page_preview=True)
async def my_submissions(u,c):
    items=get_user_services(u.effective_user.id)
    if not items: await u.message.reply_text('У вас пока нет заявок.',reply_markup=MAIN_KB); return
    await u.message.reply_text('📋 Ваши заявки:\n\n'+'\n'.join(f"#{i.id} — {i.name} — {'✅ Одобрено' if i.approved else '⏳ На проверке'}" for i in items[:20]),reply_markup=MAIN_KB)

async def add_start(u,c): c.user_data['new_service']={}; await u.message.reply_text('➕ Как хотите добавить сервис?',reply_markup=ADD_KB); return CHOOSE
async def choose(u,c):
    if u.message.contact:
        ct=u.message.contact; d={'name':(' '.join(x for x in [ct.first_name,ct.last_name] if x)).strip(),'phone':ct.phone_number,'category':'Truck Repair','address':'','languages':'','rating':None,'review_count':None,'hours':'','website':'','notes':''}; c.user_data['new_service']=d
        await u.message.reply_text('📇 Контакт получен. Теперь отправьте адрес сервиса текстом. Например:\n7509 Reese Rd, Sacramento, CA 95828'); return INPUT
    t=u.message.text
    if t=='📷 Фото / скриншот': await u.message.reply_text('📷 Отправьте фото визитки или скриншот Google Maps. Я попробую распознать данные и адрес.'); return INPUT
    if t=='✍️ Ввести вручную': await u.message.reply_text(FORM); return INPUT
    return CHOOSE

async def prepare_preview(u,c,d):
    if not d.get('address'):
        c.user_data['new_service']=d; await u.message.reply_text('📍 Не вижу адрес. Отправьте адрес сервиса текстом.'); return INPUT
    try: geo=geocode_address(d['address'])
    except Exception: logger.exception('Geocoding failed'); geo=None
    if not geo:
        c.user_data['new_service']=d; await u.message.reply_text('⚠️ Не смог определить этот адрес. Проверьте адрес и отправьте исправленную форму или адрес ещё раз.'); return INPUT
    d['latitude'],d['longitude'],_=geo; c.user_data['new_service']=d
    await u.message.reply_text('🪪 Проверьте карточку:\n\n'+card(d),reply_markup=CONFIRM_KB,disable_web_page_preview=True); return CONFIRM

async def photo_or_text(u,c):
    existing=c.user_data.get('new_service',{})
    if u.message.photo:
        path=None
        try:
            f=await u.message.photo[-1].get_file()
            with tempfile.NamedTemporaryFile(suffix='.jpg',delete=False) as tmp: path=tmp.name
            await f.download_to_drive(path)
            raw=google_vision_ocr(path)
            if not raw:
                await u.message.reply_text('⚠️ Google Vision не нашёл текста на фото. Попробуйте более чёткое фото или введите данные вручную.')
                return INPUT
            d=parse_text(raw)
            logger.info('Vision OCR recognized %d characters',len(raw))
            await u.message.reply_text('📷 Текст распознан через Google Vision. Сейчас проверю данные и адрес.')
        except Exception:
            logger.exception('Google Vision OCR failed')
            await u.message.reply_text('⚠️ Не удалось распознать фото через Google Vision. Проверьте GOOGLE_VISION_API_KEY в Railway и логи деплоя.')
            return INPUT
        finally:
            if path: Path(path).unlink(missing_ok=True)
    else:
        text=u.message.text.strip()
        if existing and ':' not in text and not existing.get('address'):
            d=existing.copy(); d['address']=text
        else: d=parse_text(text)
    return await prepare_preview(u,c,d)

async def confirm(u,c):
    if u.message.text=='✏️ Изменить': await u.message.reply_text(FORM); return INPUT
    if u.message.text!='✅ Сохранить': return CONFIRM
    d=c.user_data['new_service']; item=add_service(**d,submitted_by=u.effective_user.id,approved=u.effective_user.id in ADMIN_IDS)
    await u.message.reply_text(f"Готово. Сервис #{item.id} {'сразу одобрен ✅' if item.approved else 'отправлен на проверку ⏳'}.",reply_markup=MAIN_KB)
    if not item.approved:
        for aid in ADMIN_IDS:
            try: await c.bot.send_message(aid,'🆕 Новая заявка\n\n'+card(item_dict(item))+f'\n\nОдобрить: /approve {item.id}\nУдалить: /delete {item.id}',disable_web_page_preview=True)
            except Exception: logger.exception('admin notify')
    c.user_data.pop('new_service',None); return ConversationHandler.END
async def cancel(u,c): c.user_data.pop('new_service',None); await u.message.reply_text('Отменено.',reply_markup=MAIN_KB); return ConversationHandler.END

def is_admin(x): return x is not None and x in ADMIN_IDS
async def pending(u,c):
    if not is_admin(u.effective_user.id): return
    for i in get_pending_services()[:20]: await u.message.reply_text(card(item_dict(i))+f'\n\nОдобрить: /approve {i.id}\nУдалить: /delete {i.id}',disable_web_page_preview=True)
async def approve(u,c):
    if is_admin(u.effective_user.id) and c.args and c.args[0].isdigit(): await u.message.reply_text('✅ Одобрено.' if approve_service(int(c.args[0])) else 'Сервис не найден.')
async def delete(u,c):
    if is_admin(u.effective_user.id) and c.args and c.args[0].isdigit(): await u.message.reply_text('🗑 Удалено.' if delete_service(int(c.args[0])) else 'Сервис не найден.')
async def error_handler(u,c): logger.exception('Unhandled exception',exc_info=c.error)

def build_app():
    if not TOKEN: raise RuntimeError('TELEGRAM_BOT_TOKEN is missing')
    init_db(); app=Application.builder().token(TOKEN).build()
    conv=ConversationHandler(entry_points=[MessageHandler(filters.Regex(r'^➕ Добавить сервис$'),add_start),CommandHandler('add',add_start)],states={CHOOSE:[MessageHandler(filters.CONTACT,choose),MessageHandler(filters.TEXT & ~filters.COMMAND,choose)],INPUT:[MessageHandler(filters.PHOTO,photo_or_text),MessageHandler(filters.TEXT & ~filters.COMMAND,photo_or_text)],CONFIRM:[MessageHandler(filters.TEXT & ~filters.COMMAND,confirm)]},fallbacks=[CommandHandler('cancel',cancel),MessageHandler(filters.Regex(r'^❌ Отмена$'),cancel)])
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('help',help_cmd)); app.add_handler(CommandHandler('pending',pending)); app.add_handler(CommandHandler('approve',approve)); app.add_handler(CommandHandler('delete',delete)); app.add_handler(conv); app.add_handler(MessageHandler(filters.Regex(r'^📍 Найти рядом$'),ask_location)); app.add_handler(MessageHandler(filters.LOCATION,search_location)); app.add_handler(MessageHandler(filters.Regex(r'^📋 Мои заявки$'),my_submissions)); app.add_handler(MessageHandler(filters.Regex(r'^ℹ️ Помощь$'),help_cmd)); app.add_error_handler(error_handler); return app
if __name__=='__main__': build_app().run_polling(allowed_updates=Update.ALL_TYPES)

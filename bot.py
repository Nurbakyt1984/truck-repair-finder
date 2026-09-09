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
GOOGLE_MAPS_API_KEY=os.getenv('GOOGLE_MAPS_API_KEY','').strip()
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

TEMP_KEY = "_temp_message_ids"

async def temp_reply(u, c, text, **kwargs):
    """Send a temporary bot prompt and remember it so the chat stays clean."""
    msg = await u.message.reply_text(text, **kwargs)
    c.user_data.setdefault(TEMP_KEY, []).append(msg.message_id)
    return msg

async def clear_temp_messages(u, c):
    ids = c.user_data.pop(TEMP_KEY, [])
    chat_id = u.effective_chat.id if u.effective_chat else None
    if not chat_id:
        return
    for mid in ids:
        try:
            await c.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            # Old/already deleted messages should never break the flow.
            pass

INVALID_NAMES = {
    '✅ сохранить', '✏️ изменить', '❌ отмена', '📷 фото / скриншот',
    '📇 отправить контакт', '✍️ ввести вручную', '📍 найти рядом',
    '➕ добавить сервис', '📋 мои заявки', 'ℹ️ помощь'
}

def valid_service_name(name: str) -> bool:
    n=(name or '').strip()
    return bool(n) and n.casefold() not in INVALID_NAMES and not _looks_like_us_address(n)

def _clean_ocr_line(line: str) -> str:
    return re.sub(r'\s+', ' ', line).strip(' \t|•·')


def _looks_like_us_address(text: str) -> bool:
    text=_clean_ocr_line(text)
    # Typical US street address: house number + street name + suffix.
    street=r'\b\d{1,6}\s+[A-Za-z0-9 .#\'-]{2,60}\s(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway|Pl|Place|Cir|Circle|Trl|Trail)\b'
    return bool(re.search(street,text,re.I))


def _extract_address(lines: list[str]) -> str:
    # Google Maps often splits an address over two OCR lines, e.g.
    # "7509 Reese Rd, Sacramento," + "CA 95828".
    for i,line in enumerate(lines):
        if not _looks_like_us_address(line):
            continue
        parts=[line]
        for j in range(i+1,min(i+3,len(lines))):
            nxt=lines[j]
            if re.search(r'\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b',nxt,re.I):
                parts.append(nxt)
                break
            if re.search(r'\b[A-Za-z .\'-]+,?\s+[A-Z]{2}\b',nxt):
                parts.append(nxt)
        address=' '.join(parts)
        address=re.sub(r'\s+,',',',address)
        address=re.sub(r',?\s+([A-Z]{2}\s+\d{5}(?:-\d{4})?)\b',r', \1',address)
        return address.strip(' ,')
    return ''


def parse_text(t):
    lines=[_clean_ocr_line(x) for x in t.splitlines() if _clean_ocr_line(x)]
    d={'category':'Truck Repair','phone':'','address':'','languages':'','rating':None,'review_count':None,'hours':'','website':'','notes':''}
    keys={'название':'name','name':'name','категория':'category','category':'category','телефон':'phone','phone':'phone','адрес':'address','address':'address','языки':'languages','languages':'languages','рейтинг':'rating','rating':'rating','отзывы':'review_count','reviews':'review_count','часы':'hours','hours':'hours','сайт':'website','website':'website','примечание':'notes','notes':'notes'}
    for line in lines:
        if ':' in line:
            k,v=line.split(':',1); key=keys.get(k.strip().lower().replace('⭐','').replace('🌐','').strip())
            if key:
                v=v.strip()
                try:
                    if key=='rating' and v:
                        m=re.search(r'[0-5](?:[.,]\d+)?',v); d[key]=float(m.group().replace(',','.')) if m else None
                    elif key=='review_count' and v:
                        nums=re.sub(r'\D','',v); d[key]=int(nums) if nums else None
                    else: d[key]=v
                except Exception: pass
    full=' '.join(lines)

    # Address: supports both one-line and wrapped Google Maps OCR.
    if not d.get('address'):
        d['address']=_extract_address(lines)

    if not d.get('phone'):
        m=re.search(r'(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}',full); d['phone']=m.group(0) if m else ''
    if not d.get('website'):
        m=re.search(r'(https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|net|org|us)\b)',full,re.I); d['website']=m.group(0) if m else ''

    # Google Maps rating/reviews forms such as "4.8 (127)" or "4,8 127 отзывов".
    if d.get('rating') is None:
        for line in lines:
            m=re.search(r'(?<!\d)([0-5][.,]\d)\s*(?:\((\d[\d,]*)\)|([\d,]+)\s*(?:reviews?|отзыв))?',line,re.I)
            if m:
                try: d['rating']=float(m.group(1).replace(',','.'))
                except Exception: pass
                count=m.group(2) or m.group(3)
                if count:
                    try: d['review_count']=int(count.replace(',',''))
                    except Exception: pass
                break
    if d.get('review_count') is None:
        m=re.search(r'\b([\d,]+)\s*(?:reviews?|отзыв(?:ов|а)?)\b',full,re.I)
        if m:
            try: d['review_count']=int(m.group(1).replace(',',''))
            except Exception: pass

    if not d.get('languages') and re.search(r'se habla espa[nñ]ol|espa[nñ]ol|spanish',full,re.I): d['languages']='Español'
    if not d.get('hours'):
        if re.search(r'24\s*hours?|24\s*/\s*7',full,re.I): d['hours']='24/7'
        else:
            # Keep useful Google Maps status, e.g. "Closed · Opens at 8:00 AM".
            for line in lines:
                if re.search(r'\b(?:open|closed|opens?|closes?|открыто|закрыто)\b',line,re.I):
                    d['hours']=line[:150]; break

    # Prefer a plausible business name instead of Google Maps UI labels or address.
    if not d.get('name'):
        skip_patterns=[
            r'^маршрут$',r'^в путь$',r'^вызов$',r'^обзор$',r'^услуги$',r'^отзывы$',r'^фото$',r'^новости$',
            r'^directions?$',r'^call$',r'^website$',r'^overview$',r'^reviews?$',r'^photos?$',
            r'^закрыто',r'^открыто',r'^closed\b',r'^open\b',r'^предложить',r'^текстовое сообщение$',
        ]
        for line in lines:
            if _looks_like_us_address(line): continue
            if re.search(r'\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b',line) and len(line)<25: continue
            if re.fullmatch(r'(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}',line): continue
            if any(re.search(p,line,re.I) for p in skip_patterns): continue
            # Avoid rating-only / tiny UI strings.
            if len(line)<3 or re.fullmatch(r'[\d\s★⭐.,()]+',line): continue
            d['name']=line[:150]; break

    # If user sent only an address as plain text, don't mistake it for the service name.
    if d.get('address') and d.get('name')==d.get('address'):
        d.pop('name',None)
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
    if not address.strip():
        return None

    # Prefer Google Maps Geocoding when configured: it usually matches the
    # same building/entrance point the user sees in Google Maps.
    if GOOGLE_MAPS_API_KEY:
        try:
            params=urlencode({'address':address,'key':GOOGLE_MAPS_API_KEY,'region':'us'})
            req=Request('https://maps.googleapis.com/maps/api/geocode/json?'+params)
            with urlopen(req,timeout=12) as r:
                data=json.loads(r.read().decode('utf-8'))
            if data.get('status')=='OK' and data.get('results'):
                result=data['results'][0]
                loc=result['geometry']['location']
                return float(loc['lat']),float(loc['lng']),result.get('formatted_address','')
            logger.warning('Google geocoding status: %s',data.get('status'))
        except Exception:
            logger.exception('Google geocoding failed; falling back to Nominatim')

    params=urlencode({'q':address,'format':'jsonv2','limit':1,'countrycodes':'us'})
    req=Request('https://nominatim.openstreetmap.org/search?'+params,headers={'User-Agent':'TruckRepairFinderBot/1.0'})
    with urlopen(req,timeout=12) as r:
        data=json.loads(r.read().decode('utf-8'))
    if not data:
        return None
    return float(data[0]['lat']),float(data[0]['lon']),data[0].get('display_name','')

async def start(u,c): await u.message.reply_text(f'👋 Добро пожаловать в Поиск Автосервисов!\n\n📍 Найти рядом — сервисы в радиусе {SEARCH_RADIUS_MILES:g} миль\n➕ Добавить сервис — сохранить новый сервис\n📋 Мои сервисы — добавленные вами сервисы\nℹ️ Помощь — справка',reply_markup=MAIN_KB)
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
    await u.message.reply_text('📋 Ваши сервисы:\n\n'+'\n'.join(f"#{i.id} — {i.name} — ✅ Активен" for i in items[:20]),reply_markup=MAIN_KB)

async def add_start(u,c): c.user_data['new_service']={}; await temp_reply(u,c,'➕ Как хотите добавить сервис?',reply_markup=ADD_KB); return CHOOSE
async def choose(u,c):
    if u.message.contact:
        ct=u.message.contact; d={'name':(' '.join(x for x in [ct.first_name,ct.last_name] if x)).strip(),'phone':ct.phone_number,'category':'Truck Repair','address':'','languages':'','rating':None,'review_count':None,'hours':'','website':'','notes':''}; c.user_data['new_service']=d
        await temp_reply(u,c,'📇 Контакт получен. Теперь отправьте адрес сервиса текстом. Например:\n7509 Reese Rd, Sacramento, CA 95828'); return INPUT
    t=u.message.text
    if t=='📷 Фото / скриншот': await temp_reply(u,c,'📷 Отправьте фото визитки или скриншот Google Maps. Я попробую распознать данные и адрес.'); return INPUT
    if t=='✍️ Ввести вручную': await temp_reply(u,c,FORM); return INPUT
    return CHOOSE

async def prepare_preview(u,c,d):
    if not d.get('address'):
        c.user_data['new_service']=d; await temp_reply(u,c,'📍 Не вижу адрес. Отправьте адрес сервиса текстом.'); return INPUT
    try: geo=geocode_address(d['address'])
    except Exception: logger.exception('Geocoding failed'); geo=None
    if not geo:
        c.user_data['new_service']=d; await temp_reply(u,c,'⚠️ Не смог определить этот адрес. Проверьте адрес и отправьте исправленную форму или адрес ещё раз.'); return INPUT
    d['latitude'],d['longitude'],_=geo; c.user_data['new_service']=d
    await temp_reply(u,c,'🪪 Проверьте карточку:\n\n'+card(d),reply_markup=CONFIRM_KB,disable_web_page_preview=True); return CONFIRM

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
            parsed=parse_text(raw)
            d=existing.copy() if existing else {}
            for k,v in parsed.items():
                if v not in ('',None): d[k]=v
            d.setdefault('category','Truck Repair')
            logger.info('Vision OCR recognized %d characters',len(raw))
            await temp_reply(u,c,'📷 Текст распознан через Google Vision. Сейчас проверю данные и адрес.')
        except Exception:
            logger.exception('Google Vision OCR failed')
            await u.message.reply_text('⚠️ Не удалось распознать фото через Google Vision. Проверьте GOOGLE_VISION_API_KEY в Railway и логи деплоя.')
            return INPUT
        finally:
            if path: Path(path).unlink(missing_ok=True)
    else:
        text=u.message.text.strip()
        if c.user_data.pop('awaiting_name',False):
            d=existing.copy()
            d['name']=text[:150]
        elif ':' not in text and _looks_like_us_address(text):
            d=existing.copy() if existing else {'category':'Truck Repair','phone':'','languages':'','rating':None,'review_count':None,'hours':'','website':'','notes':''}
            d['address']=text
        elif existing and ':' not in text and not existing.get('address'):
            d=existing.copy(); d['address']=text
        else:
            parsed=parse_text(text)
            d=existing.copy() if existing else {}
            d.update({k:v for k,v in parsed.items() if v not in ('',None)})
            d.setdefault('category','Truck Repair')
    return await prepare_preview(u,c,d)

async def confirm(u,c):
    if u.message.text=='✏️ Изменить':
        await temp_reply(u,c,FORM)
        return INPUT
    if u.message.text!='✅ Сохранить':
        return CONFIRM

    d=c.user_data.get('new_service',{}).copy()
    if not valid_service_name(d.get('name','')):
        c.user_data['new_service']=d
        await temp_reply(u,c,'⚠️ Не вижу корректное название сервиса. Отправьте только название, например: Bolt Diesel')
        c.user_data['awaiting_name']=True
        return INPUT

    # User asked for immediate saving: every added service is active right away.
    item=add_service(**d,submitted_by=u.effective_user.id,approved=True)
    await clear_temp_messages(u,c)
    c.user_data.pop('new_service',None)
    c.user_data.pop('awaiting_name',None)
    await u.message.reply_text(
        f'✅ Сервис #{item.id} сохранён и уже доступен в «📍 Найти рядом».\n\n'+card(item_dict(item)),
        reply_markup=MAIN_KB,
        disable_web_page_preview=True,
    )
    return ConversationHandler.END

async def cancel(u,c):
    await clear_temp_messages(u,c)
    c.user_data.pop('new_service',None)
    c.user_data.pop('awaiting_name',None)
    await u.message.reply_text('❌ Отменено.',reply_markup=MAIN_KB)
    return ConversationHandler.END

async def menu_find(u,c):
    await clear_temp_messages(u,c)
    c.user_data.pop('new_service',None)
    c.user_data.pop('awaiting_name',None)
    await ask_location(u,c)
    return ConversationHandler.END

async def menu_add(u,c):
    await clear_temp_messages(u,c)
    c.user_data.pop('new_service',None)
    c.user_data.pop('awaiting_name',None)
    return await add_start(u,c)

async def menu_submissions(u,c):
    await clear_temp_messages(u,c)
    c.user_data.pop('new_service',None)
    c.user_data.pop('awaiting_name',None)
    await my_submissions(u,c)
    return ConversationHandler.END

async def menu_help(u,c):
    await clear_temp_messages(u,c)
    c.user_data.pop('new_service',None)
    c.user_data.pop('awaiting_name',None)
    await help_cmd(u,c)
    return ConversationHandler.END

async def menu_start(u,c):
    await clear_temp_messages(u,c)
    c.user_data.pop('new_service',None)
    c.user_data.pop('awaiting_name',None)
    await start(u,c)
    return ConversationHandler.END

async def photo_entry(u,c):
    await clear_temp_messages(u,c)
    c.user_data['new_service']={}
    return await photo_or_text(u,c)

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
    if not TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is missing')
    init_db()
    app=Application.builder().token(TOKEN).build()

    # These handlers are deliberately placed BEFORE the generic text handlers.
    # Otherwise buttons such as Cancel / Find nearby are swallowed by an active
    # ConversationHandler state and look as if they do nothing.
    interrupt_handlers=[
        MessageHandler(filters.Regex(r'^❌ Отмена$'),cancel),
        MessageHandler(filters.Regex(r'^📍 Найти рядом$'),menu_find),
        MessageHandler(filters.Regex(r'^➕ Добавить сервис$'),menu_add),
        MessageHandler(filters.Regex(r'^📋 Мои заявки$'),menu_submissions),
        MessageHandler(filters.Regex(r'^ℹ️ Помощь$'),menu_help),
        CommandHandler('start',menu_start),
    ]

    conv=ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r'^➕ Добавить сервис$'),add_start),
            MessageHandler(filters.PHOTO,photo_entry),
            CommandHandler('add',add_start),
        ],
        states={
            CHOOSE:[
                *interrupt_handlers,
                MessageHandler(filters.CONTACT,choose),
                MessageHandler(filters.TEXT & ~filters.COMMAND,choose),
            ],
            INPUT:[
                *interrupt_handlers,
                MessageHandler(filters.PHOTO,photo_or_text),
                MessageHandler(filters.TEXT & ~filters.COMMAND,photo_or_text),
            ],
            CONFIRM:[
                *interrupt_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND,confirm),
            ],
        },
        fallbacks=[CommandHandler('cancel',cancel)],
        allow_reentry=True,
    )

    # ConversationHandler goes first so it can properly terminate an unfinished
    # add-service flow when the user presses /start or a main-menu button.
    app.add_handler(conv)
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('help',help_cmd))
    app.add_handler(CommandHandler('pending',pending))
    app.add_handler(CommandHandler('approve',approve))
    app.add_handler(CommandHandler('delete',delete))
    app.add_handler(MessageHandler(filters.Regex(r'^📍 Найти рядом$'),ask_location))
    app.add_handler(MessageHandler(filters.LOCATION,search_location))
    app.add_handler(MessageHandler(filters.Regex(r'^📋 Мои заявки$'),my_submissions))
    app.add_handler(MessageHandler(filters.Regex(r'^ℹ️ Помощь$'),help_cmd))
    app.add_error_handler(error_handler)
    return app

if __name__=='__main__':
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)

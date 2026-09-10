from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from db import (
    add_service,
    approve_service,
    delete_service,
    get_approved_services,
    get_pending_services,
    get_service,
    get_user_services,
    init_db,
)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "").strip()
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
SEARCH_RADIUS_MILES = float(os.getenv("SEARCH_RADIUS_MILES", "100"))

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("truck-repair-finder")

CHOOSE, INPUT, CONFIRM = range(3)

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📍 Найти рядом", "➕ Добавить сервис"],
        ["📋 Мои заявки", "ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)

ADMIN_MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📍 Найти рядом", "➕ Добавить сервис"],
        ["📋 Мои заявки", "ℹ️ Помощь"],
        ["🛡 Админка"],
    ],
    resize_keyboard=True,
)

ADMIN_KB = ReplyKeyboardMarkup(
    [["📋 Все сервисы"], ["⬅️ Главное меню"]],
    resize_keyboard=True,
)

ADMIN_CONFIRM_KB = ReplyKeyboardMarkup(
    [["🗑 Да, удалить"], ["❌ Не удалять"]],
    resize_keyboard=True,
)

def user_main_kb(user_id):
    return ADMIN_MAIN_KB if user_id in ADMIN_IDS else MAIN_KB


ADD_KB = ReplyKeyboardMarkup(
    [
        ["📷 Фото / скриншот"],
        [KeyboardButton("📇 Отправить контакт", request_contact=True)],
        ["✍️ Ввести вручную"],
        ["❌ Отмена"],
    ],
    resize_keyboard=True,
)

SEARCH_LOCATION_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Отправить геолокацию", request_location=True)], ["❌ Отмена"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

CONFIRM_KB = ReplyKeyboardMarkup(
    [["✅ Сохранить", "✏️ Изменить"], ["❌ Отмена"]],
    resize_keyboard=True,
)

FORM = """📝 Заполните известные поля и отправьте всё одним сообщением:

Название:
Категория: Truck Repair
Телефон:
Адрес:
Языки:
Рейтинг:
Отзывы:
Часы:
Сайт:
Примечание:

📍 Геолокацию сервиса отправлять не нужно — координаты определю по адресу.
"""


def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def clean(v):
    return (v or "").strip()


def request_json(url, *, data=None, headers=None):
    headers = headers or {}
    req = Request(url, data=data, headers=headers)
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def google_vision_ocr(image_bytes: bytes) -> str:
    if not GOOGLE_VISION_API_KEY:
        raise RuntimeError("GOOGLE_VISION_API_KEY is not configured")

    url = (
        "https://vision.googleapis.com/v1/images:annotate?"
        + urlencode({"key": GOOGLE_VISION_API_KEY})
    )
    import base64
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "TEXT_DETECTION", "maxResults": 1}],
            }
        ]
    }
    result = request_json(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    responses = result.get("responses", [])
    if not responses:
        return ""

    response = responses[0]
    if response.get("error"):
        raise RuntimeError(response["error"].get("message", "Google Vision error"))

    annotations = response.get("textAnnotations", [])
    return annotations[0].get("description", "") if annotations else ""


def google_geocode(address: str):
    """Return (lat, lon, formatted_address) using Google Geocoding API."""
    if not GOOGLE_MAPS_API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY is not configured")

    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urlencode(
        {
            "address": address,
            "key": GOOGLE_MAPS_API_KEY,
            "region": "us",
        }
    )
    data = request_json(url)
    status = data.get("status")

    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        raise RuntimeError(
            f"Google Geocoding error: {status}: {data.get('error_message', '')}"
        )

    result = data["results"][0]
    location = result["geometry"]["location"]
    return (
        float(location["lat"]),
        float(location["lng"]),
        result.get("formatted_address", address),
    )


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_name(value: str) -> str:
    value = (value or "").lower().replace("’", "'")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def names_similar(a: str, b: str) -> bool:
    a_n, b_n = normalize_name(a), normalize_name(b)
    if not a_n or not b_n:
        return True
    if a_n in b_n or b_n in a_n:
        return True
    a_words = {w for w in a_n.split() if len(w) > 2}
    b_words = {w for w in b_n.split() if len(w) > 2}
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / max(1, min(len(a_words), len(b_words)))
    return overlap >= 0.6


def google_place_lookup(name: str = "", phone: str = "", address: str = ""):
    """Resolve a business through Places and reject obvious wrong matches."""
    if not GOOGLE_MAPS_API_KEY:
        return None

    parts = [clean(name), clean(phone), clean(address)]
    query = " ".join(x for x in parts if x).strip()
    if not query:
        return None

    url = "https://places.googleapis.com/v1/places:searchText"
    payload = {
        "textQuery": query[:500],
        "maxResultCount": 3,
        "languageCode": "en",
        "regionCode": "US",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,places.location,"
            "places.nationalPhoneNumber,places.rating,places.userRatingCount,"
            "places.websiteUri,places.regularOpeningHours"
        ),
    }

    try:
        data = request_json(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
    except Exception:
        logger.exception("Google Places lookup failed")
        return None

    input_phone = normalize_phone(phone)
    for p in data.get("places") or []:
        p_name = (p.get("displayName") or {}).get("text", "")
        p_phone = p.get("nationalPhoneNumber", "")
        place_phone = normalize_phone(p_phone)

        # If OCR gave us a phone, never accept a different business phone.
        if input_phone and place_phone and input_phone != place_phone:
            continue
        if name and p_name and not names_similar(name, p_name):
            continue

        location = p.get("location") or {}
        hours = p.get("regularOpeningHours") or {}
        return {
            "name": p_name,
            "address": p.get("formattedAddress", ""),
            "phone": p_phone,
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "rating": p.get("rating"),
            "review_count": p.get("userRatingCount"),
            "website": p.get("websiteUri", ""),
            "hours": "; ".join(hours.get("weekdayDescriptions") or []),
        }
    return None


def parse_rating(text):
    # Only accept an explicit star/rating context. Never treat a phone/address digit as rating.
    patterns = [
        r"(?:★|⭐)\s*([1-5](?:[.,]\d)?)",
        r"\b([1-5](?:[.,]\d)?)\s*(?:★|⭐|stars?|зв[её]зд)",
        r"(?:rating|рейтинг)\s*[:\-]?\s*([1-5](?:[.,]\d)?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    return None


def parse_reviews(text):
    # Reviews must be explicitly labeled; do not read arbitrary numbers in parentheses.
    patterns = [
        r"([\d,.\s]+)\s*(?:reviews?|отзыв(?:ов|а)?|ratings?)",
        r"(?:reviews?|отзыв(?:ов|а)?|ratings?)\s*[:\-]?\s*([\d,.\s]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits:
                try:
                    return int(digits)
                except ValueError:
                    pass
    return None


def parse_phone(text):
    candidates = re.findall(
        r"(?:\+?1[\s.\-()]*)?(?:\(?\d{3}\)?[\s.\-]*)\d{3}[\s.\-]*\d{4}",
        text,
    )
    return candidates[0].strip() if candidates else ""


def parse_website(text):
    m = re.search(
        r"(https?://[^\s]+|www\.[^\s]+|[A-Za-z0-9.-]+\.(?:com|net|org|us|biz)(?:/[^\s]*)?)",
        text,
        re.I,
    )
    return m.group(1).rstrip(".,)") if m else ""


def detect_languages(text):
    found = []
    low = text.lower()
    markers = [
        ("Español", ["se habla español", "se habla espanol", "spanish", "español"]),
        ("Русский", ["русский", "russian"]),
        ("Кыргызча", ["кыргыз", "kyrgyz"]),
        ("O‘zbekcha", ["uzbek", "o'zbek", "o‘zbek", "ўзбек"]),
    ]
    for language, words in markers:
        if any(word in low for word in words):
            found.append(language)
    return ", ".join(found)


def detect_hours(text):
    low = text.lower()
    if any(x in low for x in ["24/7", "24 hours", "24hrs", "24 hrs", "24 hour"]):
        return "24/7"
    return ""


def detect_category(text):
    low = text.lower()
    if any(x in low for x in ["road service", "roadside", "mobile repair"]):
        return "Roadside Service"
    if any(x in low for x in ["tire", "tyre"]):
        return "Truck Tire Service"
    if any(x in low for x in ["diesel", "truck repair", "mechanic"]):
        return "Truck Repair"
    if "towing" in low or "tow " in low:
        return "Towing"
    return "Truck Repair"


def looks_like_ui_line(line: str) -> bool:
    low = line.lower().strip()
    ui_terms = [
        "маршрут", "в путь", "вызов", "обзор", "услуги", "отзывы", "фото", "новости",
        "предложить правку", "текстовое сообщение", "закрыто", "откроется",
        "directions", "call", "overview", "photos", "reviews", "suggest an edit",
        "supplier", "поставщик пропана", "продажа автомобилей",
    ]
    return any(term in low for term in ui_terms)


def guess_name(text):
    lines = [clean(x) for x in text.splitlines() if clean(x)]
    # Strong preference for business-like lines near the top, including "Road Service".
    for line in lines[:12]:
        if len(line) > 80 or looks_like_ui_line(line):
            continue
        if parse_phone(line):
            continue
        if re.search(r"\b\d{1,5}\s+[A-Za-z0-9].*(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Drive|Dr|Lane|Ln|Way|Hwy|Highway)\b", line, re.I):
            continue
        if re.search(r"\b(?:service|repair|diesel|tire|towing|garage|truck|auto)\b", line, re.I):
            return line[:150]
    for line in lines[:12]:
        if len(line) <= 80 and not looks_like_ui_line(line) and not parse_phone(line):
            return line[:150]
    return ""


def extract_address_from_ocr(text: str) -> str:
    lines = [clean(x) for x in text.splitlines() if clean(x)]
    street = re.compile(
        r"\b\d{1,6}\s+.+?\b(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway)\b",
        re.I,
    )
    for i, line in enumerate(lines):
        if street.search(line):
            candidate = line
            if i + 1 < len(lines) and re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", lines[i + 1]):
                candidate += ", " + lines[i + 1]
            return candidate[:300]
    return ""


def extract_notes(text: str, name: str, phone: str, address: str) -> str:
    useful = []
    for raw in text.splitlines():
        line = clean(raw)
        if not line or looks_like_ui_line(line):
            continue
        if line == name or (phone and normalize_phone(line) == normalize_phone(phone)):
            continue
        if address and line in address:
            continue
        if re.search(r"\b(?:new and used tires|construction machines|trucks?|rvs?|cars?|trailers?|forklifts?)\b", line, re.I):
            useful.append(line)
    # Remove duplicates while keeping order.
    out = []
    seen = set()
    for x in useful:
        key = x.lower()
        if key not in seen:
            seen.add(key)
            out.append(x)
    return "; ".join(out[:6])[:500]


def parse_labeled_form(text):
    aliases = {
        "название": "name", "name": "name",
        "категория": "category", "category": "category",
        "телефон": "phone", "phone": "phone",
        "адрес": "address", "address": "address",
        "языки": "languages", "languages": "languages",
        "рейтинг": "rating", "rating": "rating",
        "отзывы": "review_count", "reviews": "review_count",
        "часы": "hours", "часы работы": "hours", "hours": "hours",
        "сайт": "website", "website": "website",
        "примечание": "notes", "notes": "notes",
    }
    result = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        field = aliases.get(key.strip().lower())
        if field and value.strip():
            result[field] = value.strip()
    if "rating" in result:
        try:
            result["rating"] = float(str(result["rating"]).replace(",", "."))
        except ValueError:
            result["rating"] = None
    if "review_count" in result:
        try:
            result["review_count"] = int(re.sub(r"\D", "", str(result["review_count"])))
        except ValueError:
            result["review_count"] = None
    return result


def enrich_draft_with_place(draft: dict) -> dict:
    place = google_place_lookup(
        draft.get("name", ""),
        draft.get("phone", ""),
        draft.get("address", ""),
    )
    if not place:
        return draft
    for field in ("name", "address", "phone", "rating", "review_count", "website", "hours"):
        value = place.get(field)
        if value not in (None, ""):
            draft[field] = value
    if place.get("latitude") is not None and place.get("longitude") is not None:
        draft["latitude"] = float(place["latitude"])
        draft["longitude"] = float(place["longitude"])
    return draft


def parse_ocr(text):
    name = guess_name(text)
    phone = parse_phone(text)
    address = extract_address_from_ocr(text)
    result = {
        "name": name,
        "category": detect_category(text),
        "phone": phone,
        "address": address,
        "languages": detect_languages(text),
        "rating": parse_rating(text),
        "review_count": parse_reviews(text),
        "hours": detect_hours(text),
        "website": parse_website(text),
        "notes": extract_notes(text, name, phone, address),
    }
    return enrich_draft_with_place(result)

def format_hours(hours: str) -> str:
    """Show Google weekday descriptions as a clean vertical list."""
    hours = clean(hours)
    if not hours:
        return ""

    # Google Places returns descriptions joined with '; '.
    parts = [clean(x) for x in hours.split(";") if clean(x)]
    if len(parts) <= 1:
        return hours

    day_map = {
        "Monday": "Mon",
        "Tuesday": "Tue",
        "Wednesday": "Wed",
        "Thursday": "Thu",
        "Friday": "Fri",
        "Saturday": "Sat",
        "Sunday": "Sun",
    }
    rows = []
    for item in parts:
        day, sep, value = item.partition(":")
        short = day_map.get(day.strip(), day.strip()[:3])
        value = value.strip() if sep else item.strip()
        # Make closed days easy to scan.
        if value.lower() == "closed":
            value = "Closed"
        rows.append(f"{short:<3}  {value}")
    return "\n".join(rows)


def preview(d):
    rating = d.get("rating")
    reviews = d.get("review_count")

    lines = [
        f"🔧 {clean(d.get('name')) or 'Название не указано'}",
        "━━━━━━━━━━━━━━",
    ]

    if rating is not None:
        rating_text = f"⭐ {rating}/5"
        if reviews is not None:
            rating_text += f"  •  {reviews} отзывов"
        lines.append(rating_text)

    lines.append(f"🏷 {clean(d.get('category')) or 'Truck Repair'}")

    if clean(d.get("address")):
        lines.extend(["", "📍 Адрес", clean(d.get("address"))])
    if clean(d.get("phone")):
        lines.extend(["", "📞 Телефон", clean(d.get("phone"))])
    if clean(d.get("languages")):
        lines.extend(["", "🌐 Языки", clean(d.get("languages"))])

    pretty_hours = format_hours(d.get("hours", ""))
    if pretty_hours:
        lines.extend(["", "🕐 Часы работы", pretty_hours])

    if clean(d.get("website")):
        lines.extend(["", "🌍 Сайт", clean(d.get("website"))])
    if clean(d.get("notes")):
        lines.extend(["", "📝 Примечание", clean(d.get("notes"))])

    lines.extend(["", "━━━━━━━━━━━━━━", "✅ Всё правильно?"])
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Добро пожаловать в Поиск Автосервисов!\n\n"
        "📍 Найти рядом — сервисы в радиусе 100 миль\n"
        "➕ Добавить сервис — добавить новый сервис\n"
        "📋 Мои заявки — ваши добавленные сервисы\n"
        "ℹ️ Помощь — справка",
        reply_markup=user_main_kb(update.effective_user.id if update.effective_user else 0),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 «Найти рядом» — отправьте свою геолокацию.\n"
        "➕ «Добавить сервис» — можно отправить фото/скриншот, контакт или заполнить форму.\n\n"
        "При добавлении сервиса его геолокацию отправлять не нужно: "
        "бот определяет координаты по адресу.",
        reply_markup=user_main_kb(update.effective_user.id if update.effective_user else 0),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("draft", None)
    context.user_data.pop("awaiting_field", None)
    uid = update.effective_user.id if update.effective_user else 0
    await update.message.reply_text("Отменено.", reply_markup=user_main_kb(uid))
    return ConversationHandler.END


async def begin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("draft", None)
    context.user_data.pop("awaiting_field", None)
    await update.message.reply_text(
        "➕ Как хотите добавить сервис?\n\n"
        "📷 Можно отправить визитку или скриншот Google Maps.\n"
        "📇 Можно отправить контакт.\n"
        "✍️ Или заполнить всё одним сообщением.",
        reply_markup=ADD_KB,
    )
    return CHOOSE


async def choose_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = clean(update.message.text)

    if text == "❌ Отмена":
        return await cancel(update, context)

    if text == "📷 Фото / скриншот":
        await update.message.reply_text(
            "📷 Отправьте фото визитки или скриншот сервиса.\n"
            "Я постараюсь определить название, телефон, точный адрес, рейтинг и другие данные.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return INPUT

    if text == "✍️ Ввести вручную":
        await update.message.reply_text(FORM, reply_markup=ReplyKeyboardRemove())
        return INPUT

    # В режиме добавления разрешаем сразу прислать адрес текстом.
    # Бот сначала пытается найти конкретный сервис через Google Places.
    if re.search(r"\d{1,6}\s+.+(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway)\b", text, re.I):
        await update.message.reply_text(
            "🔎 Ищу сервис по этому адресу в Google…",
            reply_markup=ReplyKeyboardRemove(),
        )
        place = google_place_lookup(address=text)
        if place:
            draft = {
                "name": place.get("name", ""),
                "category": "Truck Repair",
                "phone": place.get("phone", ""),
                "address": place.get("address", text),
                "languages": "",
                "rating": place.get("rating"),
                "review_count": place.get("review_count"),
                "hours": place.get("hours", ""),
                "website": place.get("website", ""),
                "notes": "",
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
            }
            return await prepare_preview(update, context, draft)

        # Если бизнес по адресу не найден, всё равно проверяем сам адрес.
        draft = {
            "name": "",
            "category": "Truck Repair",
            "phone": "",
            "address": text,
            "languages": "",
            "rating": None,
            "review_count": None,
            "hours": "",
            "website": "",
            "notes": "",
        }
        return await prepare_preview(update, context, draft)

    await update.message.reply_text(
        "Выберите один из вариантов кнопками ниже или просто отправьте адрес сервиса текстом.",
        reply_markup=ADD_KB,
    )
    return CHOOSE


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.message.contact
    name = " ".join(x for x in [c.first_name, c.last_name] if x).strip()
    context.user_data["draft"] = {
        "name": name,
        "category": "Truck Repair",
        "phone": c.phone_number or "",
        "address": "",
        "languages": "",
        "rating": None,
        "review_count": None,
        "hours": "",
        "website": "",
        "notes": "",
    }
    context.user_data["awaiting_field"] = "address"
    await update.message.reply_text(
        "📍 Теперь отправьте только адрес сервиса текстом.\n"
        "Например: 7509 Reese Rd, Sacramento, CA 95828",
        reply_markup=ReplyKeyboardRemove(),
    )
    return INPUT


async def prepare_preview(update, context, draft):
    # Re-check Places whenever new address/name data was supplied.
    draft = enrich_draft_with_place(draft)
    address = clean(draft.get("address"))

    # If Places did not already give us coordinates, geocode the address.
    if draft.get("latitude") is None or draft.get("longitude") is None:
        if not address:
            context.user_data["draft"] = draft
            context.user_data["awaiting_field"] = "address"
            await update.message.reply_text(
                "📍 Мне не удалось определить адрес автоматически.\n"
                "Отправьте только адрес сервиса текстом.\n\n"
                "Например: 7509 Reese Rd, Sacramento, CA 95828"
            )
            return INPUT

        try:
            geo = google_geocode(address)
        except Exception:
            logger.exception("Google geocoding failed")
            context.user_data["draft"] = draft
            context.user_data["awaiting_field"] = "address"
            await update.message.reply_text(
                "⚠️ Google не смог проверить адрес. Проверьте GOOGLE_MAPS_API_KEY "
                "и что Geocoding API включён."
            )
            return INPUT

        if not geo:
            context.user_data["draft"] = draft
            context.user_data["awaiting_field"] = "address"
            await update.message.reply_text(
                "❌ Не удалось найти этот адрес.\n"
                "Проверьте улицу, город, штат и ZIP и отправьте адрес ещё раз."
            )
            return INPUT

        draft["latitude"], draft["longitude"], draft["address"] = geo

    if not clean(draft.get("name")):
        context.user_data["draft"] = draft
        context.user_data["awaiting_field"] = "name"
        await update.message.reply_text(
            "Название сервиса не найдено. Отправьте только название сервиса."
        )
        return INPUT

    context.user_data["draft"] = draft
    context.user_data.pop("awaiting_field", None)
    await update.message.reply_text(preview(draft), reply_markup=CONFIRM_KB)
    return CONFIRM


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Распознаю фото и ищу сервис в Google…")

    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            path = tmp.name

        try:
            await tg_file.download_to_drive(path)
            with open(path, "rb") as f:
                image_bytes = f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        text = google_vision_ocr(image_bytes)

        if not text.strip():
            await update.message.reply_text(
                "❌ Google Vision не нашёл текст на фото.\n"
                "Попробуйте более чёткое фото или введите данные вручную.",
                reply_markup=ADD_KB,
            )
            return CHOOSE

        logger.info("OCR text: %s", text[:1500])
        draft = parse_ocr(text)
        return await prepare_preview(update, context, draft)

    except Exception as exc:
        logger.exception("Photo recognition failed")
        await update.message.reply_text(
            "⚠️ Не удалось обработать фото.\n"
            "Проверьте, что GOOGLE_VISION_API_KEY в Railway правильный "
            "и Cloud Vision API включён.\n\n"
            f"Ошибка: {str(exc)[:250]}",
            reply_markup=ADD_KB,
        )
        return CHOOSE


async def handle_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = clean(update.message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)

    old = context.user_data.get("draft")
    awaiting = context.user_data.get("awaiting_field")

    # When the bot explicitly asked for one missing field, the next plain
    # message is that field. This prevents an address from being mistaken
    # for the full manual form.
    if old and awaiting == "address":
        old["address"] = text
        old.pop("latitude", None)
        old.pop("longitude", None)
        context.user_data.pop("awaiting_field", None)
        return await prepare_preview(update, context, old)

    if old and awaiting == "name":
        old["name"] = text
        context.user_data.pop("awaiting_field", None)
        return await prepare_preview(update, context, old)

    # Backward-compatible fallback for drafts created before awaiting_field.
    if old and not clean(old.get("address")) and ":" not in text:
        old["address"] = text
        old.pop("latitude", None)
        old.pop("longitude", None)
        return await prepare_preview(update, context, old)

    if old and not clean(old.get("name")) and ":" not in text:
        old["name"] = text
        return await prepare_preview(update, context, old)

    draft = parse_labeled_form(text)

    if not draft:
        await update.message.reply_text(
            "Пожалуйста, заполните форму одним сообщением:\n\n" + FORM
        )
        return INPUT

    draft.setdefault("category", "Truck Repair")
    draft.setdefault("phone", "")
    draft.setdefault("address", "")
    draft.setdefault("languages", "")
    draft.setdefault("rating", None)
    draft.setdefault("review_count", None)
    draft.setdefault("hours", "")
    draft.setdefault("website", "")
    draft.setdefault("notes", "")

    return await prepare_preview(update, context, draft)


async def edit_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data.get("draft", {})
    form = (
        "📝 Измените нужные поля и отправьте всё сообщение:\n\n"
        f"Название: {clean(draft.get('name'))}\n"
        f"Категория: {clean(draft.get('category')) or 'Truck Repair'}\n"
        f"Телефон: {clean(draft.get('phone'))}\n"
        f"Адрес: {clean(draft.get('address'))}\n"
        f"Языки: {clean(draft.get('languages'))}\n"
        f"Рейтинг: {'' if draft.get('rating') is None else draft.get('rating')}\n"
        f"Отзывы: {'' if draft.get('review_count') is None else draft.get('review_count')}\n"
        f"Часы: {clean(draft.get('hours'))}\n"
        f"Сайт: {clean(draft.get('website'))}\n"
        f"Примечание: {clean(draft.get('notes'))}"
    )
    await update.message.reply_text(form, reply_markup=ReplyKeyboardRemove())
    return INPUT


async def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text("Заявка не найдена.", reply_markup=MAIN_KB)
        return ConversationHandler.END

    # Services are immediately available. No approval queue.
    service = add_service(
        name=clean(draft.get("name")),
        category=clean(draft.get("category")) or "Truck Repair",
        phone=clean(draft.get("phone")),
        address=clean(draft.get("address")),
        latitude=float(draft["latitude"]),
        longitude=float(draft["longitude"]),
        notes=clean(draft.get("notes")),
        submitted_by=update.effective_user.id if update.effective_user else None,
        approved=True,
        languages=clean(draft.get("languages")),
        rating=draft.get("rating"),
        review_count=draft.get("review_count"),
        hours=clean(draft.get("hours")),
        website=clean(draft.get("website")),
    )

    context.user_data.pop("draft", None)
    await update.message.reply_text(
        f"✅ Готово! Сервис #{service.id} сохранён и уже доступен в поиске.",
        reply_markup=user_main_kb(update.effective_user.id if update.effective_user else 0),
    )
    return ConversationHandler.END


async def my_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    services = get_user_services(update.effective_user.id)
    if not services:
        await update.message.reply_text(
            "📋 У вас пока нет добавленных сервисов.",
            reply_markup=user_main_kb(update.effective_user.id if update.effective_user else 0),
        )
        return

    lines = ["📋 Ваши сервисы:"]
    for s in services[:30]:
        status = "✅ Сохранён" if s.approved else "⏳ На проверке"
        lines.append(f"#{s.id} — {s.name} — {status}")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)


async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Отправьте вашу геолокацию, чтобы найти сервисы рядом:",
        reply_markup=SEARCH_LOCATION_KB,
    )


async def find_nearby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    if not loc:
        return

    found = []
    for s in get_approved_services():
        distance = haversine_miles(
            loc.latitude,
            loc.longitude,
            s.latitude,
            s.longitude,
        )
        if distance <= SEARCH_RADIUS_MILES:
            found.append((distance, s))

    if not found:
        await update.message.reply_text(
            f"В радиусе {SEARCH_RADIUS_MILES:g} миль сервисов пока не найдено.",
            reply_markup=user_main_kb(update.effective_user.id if update.effective_user else 0),
        )
        return

    found.sort(key=lambda item: (round(item[0], 1), -(item[1].rating or 0)))

    await update.message.reply_text(
        f"🔧 Найдено сервисов: {len(found)}",
        reply_markup=user_main_kb(update.effective_user.id if update.effective_user else 0),
    )

    for distance, s in found[:15]:
        lines = [
            f"🔧 {s.name}",
            "━━━━━━━━━━━━━━",
        ]

        if s.rating is not None:
            rating_text = f"⭐ {s.rating}/5"
            if s.review_count is not None:
                rating_text += f"  •  {s.review_count} отзывов"
            lines.append(rating_text)

        lines.append(f"🏷 {s.category}")

        if s.address:
            lines.extend(["", "📍 Адрес", s.address])
        if s.phone:
            lines.extend(["", "📞 Телефон", s.phone])
        if getattr(s, "languages", ""):
            lines.extend(["", "🌐 Языки", s.languages])

        pretty_hours = format_hours(getattr(s, "hours", ""))
        if pretty_hours:
            lines.extend(["", "🕐 Часы работы", pretty_hours])

        if getattr(s, "website", ""):
            lines.extend(["", "🌍 Сайт", s.website])

        lines.extend(["", f"📏 Расстояние: {distance:.1f} mi"])
        route = (
            "https://www.google.com/maps/dir/?api=1&"
            + urlencode({"destination": f"{s.latitude},{s.longitude}"})
        )
        lines.append(f"🗺 Маршрут: {route}")

        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.", reply_markup=MAIN_KB)
        return
    await update.message.reply_text(
        "🛡 Админка\n\nЗдесь можно посмотреть сохранённые сервисы и удалить плохой или ошибочный сервис.",
        reply_markup=ADMIN_KB,
    )


async def admin_list_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.", reply_markup=MAIN_KB)
        return
    services = get_approved_services()
    if not services:
        await update.message.reply_text("В базе пока нет сервисов.", reply_markup=ADMIN_KB)
        return
    lines = ["📋 Сервисы в базе:"]
    for svc in services[:50]:
        lines.append(f"#{svc.id} — {svc.name} — {svc.address or 'без адреса'}")
    lines.append("\nЧтобы удалить, отправьте: Удалить ID\nНапример: Удалить 12")
    await update.message.reply_text("\n".join(lines), reply_markup=ADMIN_KB)


async def admin_request_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        return
    m = re.fullmatch(r"(?:удалить|delete)\s+#?(\d+)", clean(update.message.text), re.I)
    if not m:
        return
    sid = int(m.group(1))
    svc = get_service(sid)
    if not svc:
        await update.message.reply_text("Сервис с таким ID не найден.", reply_markup=ADMIN_KB)
        return
    context.user_data["admin_delete_id"] = sid
    await update.message.reply_text(
        f"⚠️ Точно удалить сервис?\n\n🔧 {svc.name}\n📞 {svc.phone or '-'}\n📍 {svc.address or '-'}",
        reply_markup=ADMIN_CONFIRM_KB,
    )


async def admin_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        return
    sid = context.user_data.pop("admin_delete_id", None)
    if not sid:
        await update.message.reply_text("Нет выбранного сервиса.", reply_markup=ADMIN_KB)
        return
    if delete_service(sid):
        await update.message.reply_text(f"🗑 Сервис #{sid} удалён из базы.", reply_markup=ADMIN_KB)
    else:
        await update.message.reply_text("Сервис уже удалён или не найден.", reply_markup=ADMIN_KB)


async def admin_cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("admin_delete_id", None)
    await update.message.reply_text("Удаление отменено.", reply_markup=ADMIN_KB)


async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Главное меню.",
        reply_markup=user_main_kb(update.effective_user.id if update.effective_user else 0),
    )


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    services = get_pending_services()
    if not services:
        await update.message.reply_text("Заявок на проверке нет.")
        return

    lines = ["⏳ Заявки на проверке:"]
    for s in services:
        lines.append(f"#{s.id} — {s.name} — {s.address}")
    await update.message.reply_text("\n".join(lines))


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /approve ID")
        return
    sid = int(context.args[0])
    if approve_service(sid):
        await update.message.reply_text(f"✅ Сервис #{sid} одобрен.")
    else:
        await update.message.reply_text("Сервис не найден.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /delete ID")
        return
    sid = int(context.args[0])
    if delete_service(sid):
        await update.message.reply_text(f"🗑 Сервис #{sid} удалён.")
    else:
        await update.message.reply_text("Сервис не найден.")


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(TOKEN).build()

    add_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^➕ Добавить сервис$"), begin_add),
            MessageHandler(filters.PHOTO, handle_photo),
        ],
        states={
            CHOOSE: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.CONTACT, handle_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, choose_add),
            ],
            INPUT: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.CONTACT, handle_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_text),
            ],
            CONFIRM: [
                MessageHandler(filters.Regex(r"^✅ Сохранить$"), save_draft),
                MessageHandler(filters.Regex(r"^✏️ Изменить$"), edit_draft),
                MessageHandler(filters.Regex(r"^❌ Отмена$"), cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))

    app.add_handler(add_conversation)

    app.add_handler(
        MessageHandler(filters.Regex(r"^📍 Найти рядом$"), ask_location)
    )
    app.add_handler(
        MessageHandler(filters.LOCATION, find_nearby)
    )
    app.add_handler(
        MessageHandler(filters.Regex(r"^📋 Мои заявки$"), my_services)
    )
    app.add_handler(
        MessageHandler(filters.Regex(r"^ℹ️ Помощь$"), help_cmd)
    )
    app.add_handler(
        MessageHandler(filters.Regex(r"^❌ Отмена$"), cancel)
    )

    app.add_handler(MessageHandler(filters.Regex(r"^🛡 Админка$"), admin_menu))
    app.add_handler(MessageHandler(filters.Regex(r"^📋 Все сервисы$"), admin_list_services))
    app.add_handler(MessageHandler(filters.Regex(r"^(?i:Удалить|delete)\s+#?\d+$"), admin_request_delete))
    app.add_handler(MessageHandler(filters.Regex(r"^🗑 Да, удалить$"), admin_confirm_delete))
    app.add_handler(MessageHandler(filters.Regex(r"^❌ Не удалять$"), admin_cancel_delete))
    app.add_handler(MessageHandler(filters.Regex(r"^⬅️ Главное меню$"), back_main))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

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

(
    ADD_NAME,
    ADD_CATEGORY,
    ADD_PHONE,
    ADD_ADDRESS,
    ADD_LOCATION,
    ADD_NOTES,
) = range(6)

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📍 Найти рядом", "➕ Добавить сервис"],
        ["📋 Мои заявки", "ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)

LOCATION_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Отправить геолокацию", request_location=True)], ["❌ Отмена"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

CATEGORIES = ReplyKeyboardMarkup(
    [
        ["Truck Repair", "Tire Shop"],
        ["Trailer Repair", "Mobile Mechanic"],
        ["Roadside Service", "Towing"],
        ["❌ Отмена"],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def maps_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"


def service_text(item, distance: float | None = None) -> str:
    parts = [f"🔧 {item.name}", f"🏷 {item.category}"]
    if distance is not None:
        parts.append(f"📏 {distance:.1f} mi")
    if item.phone:
        parts.append(f"📞 {item.phone}")
    if item.address:
        parts.append(f"📍 {item.address}")
    if item.notes:
        parts.append(f"📝 {item.notes}")
    parts.append(f"🗺 Маршрут: {maps_link(item.latitude, item.longitude)}")
    return "\n".join(parts)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Добро пожаловать в Truck Repair Finder!\n\n"
        "Я помогу найти ближайший ремонт грузовиков, шиномонтаж, "
        "ремонт прицепов, mobile mechanic и roadside service.\n\n"
        "Выберите действие:"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KB)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ Как пользоваться:\n\n"
        "📍 Найти рядом — отправьте геолокацию, и бот покажет ближайшие проверенные сервисы.\n"
        "➕ Добавить сервис — предложите новый сервис. Он появится после одобрения админом.\n"
        "📋 Мои заявки — ваши добавленные сервисы и их статус.\n\n"
        "Команды: /start, /help, /cancel",
        reply_markup=MAIN_KB,
    )


async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"📍 Отправьте вашу геолокацию. Я покажу сервисы в радиусе до {SEARCH_RADIUS_MILES:g} миль.",
        reply_markup=LOCATION_KB,
    )


async def handle_search_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    services = get_approved_services()

    if not services:
        await update.message.reply_text(
            "Пока в базе нет одобренных сервисов. Вы можете добавить первый через «➕ Добавить сервис».",
            reply_markup=MAIN_KB,
        )
        return

    nearby = []
    for item in services:
        distance = miles_between(loc.latitude, loc.longitude, item.latitude, item.longitude)
        if distance <= SEARCH_RADIUS_MILES:
            nearby.append((distance, item))

    nearby.sort(key=lambda x: x[0])

    if not nearby:
        await update.message.reply_text(
            f"В радиусе {SEARCH_RADIUS_MILES:g} миль пока ничего не найдено.",
            reply_markup=MAIN_KB,
        )
        return

    await update.message.reply_text(
        f"🔎 Найдено сервисов: {len(nearby)}. Показываю ближайшие:",
        reply_markup=MAIN_KB,
    )

    for distance, item in nearby[:10]:
        await update.message.reply_text(
            service_text(item, distance),
            disable_web_page_preview=True,
        )


async def my_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    items = get_user_services(user_id)
    if not items:
        await update.message.reply_text("У вас пока нет заявок.", reply_markup=MAIN_KB)
        return

    lines = ["📋 Ваши заявки:\n"]
    for item in items[:20]:
        status = "✅ Одобрено" if item.approved else "⏳ На проверке"
        lines.append(f"#{item.id} — {item.name} — {status}")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_service"] = {}
    await update.message.reply_text(
        "Введите название сервиса:",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_service"]["name"] = update.message.text.strip()
    await update.message.reply_text("Выберите категорию:", reply_markup=CATEGORIES)
    return ADD_CATEGORY


async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_service"]["category"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите номер телефона сервиса.\nЕсли не знаете — отправьте «-».",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return ADD_PHONE


async def add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    context.user_data["new_service"]["phone"] = "" if value == "-" else value
    await update.message.reply_text(
        "Введите адрес сервиса.\nЕсли не знаете — отправьте «-»."
    )
    return ADD_ADDRESS


async def add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    context.user_data["new_service"]["address"] = "" if value == "-" else value
    await update.message.reply_text(
        "Теперь отправьте геолокацию самого сервиса:",
        reply_markup=LOCATION_KB,
    )
    return ADD_LOCATION


async def add_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    context.user_data["new_service"]["latitude"] = loc.latitude
    context.user_data["new_service"]["longitude"] = loc.longitude
    await update.message.reply_text(
        "Добавьте короткую заметку (например: 24/7, truck tires, mobile service).\n"
        "Если нечего добавить — отправьте «-».",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return ADD_NOTES


async def add_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data.get("new_service", {})
    notes = update.message.text.strip()
    data["notes"] = "" if notes == "-" else notes

    item = add_service(
        **data,
        submitted_by=update.effective_user.id,
        approved=update.effective_user.id in ADMIN_IDS,
    )

    status = "сразу одобрен ✅" if item.approved else "отправлен на проверку ⏳"
    await update.message.reply_text(
        f"Готово. Сервис #{item.id} {status}.",
        reply_markup=MAIN_KB,
    )

    # Notify admins
    if not item.approved:
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    "🆕 Новая заявка на сервис\n\n"
                    + service_text(item)
                    + f"\n\nОдобрить: /approve {item.id}\nУдалить: /delete {item.id}",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Could not notify admin %s", admin_id)

    context.user_data.pop("new_service", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_service", None)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
    return ConversationHandler.END


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    items = get_pending_services()
    if not items:
        await update.message.reply_text("Нет заявок на проверке.")
        return
    for item in items[:20]:
        await update.message.reply_text(
            service_text(item)
            + f"\n\nОдобрить: /approve {item.id}\nУдалить: /delete {item.id}",
            disable_web_page_preview=True,
        )


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /approve 123")
        return
    service_id = int(context.args[0])
    ok = approve_service(service_id)
    await update.message.reply_text("✅ Одобрено." if ok else "Сервис не найден.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /delete 123")
        return
    service_id = int(context.args[0])
    ok = delete_service(service_id)
    await update.message.reply_text("🗑 Удалено." if ok else "Сервис не найден.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and add your token."
        )

    init_db()
    app = Application.builder().token(TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^➕ Добавить сервис$"), add_start),
            CommandHandler("add", add_start),
        ],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)],
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_phone)],
            ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_address)],
            ADD_LOCATION: [MessageHandler(filters.LOCATION, add_location)],
            ADD_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(r"^❌ Отмена$"), cancel),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(conversation)

    app.add_handler(MessageHandler(filters.Regex(r"^📍 Найти рядом$"), ask_location))
    app.add_handler(MessageHandler(filters.LOCATION, handle_search_location))
    app.add_handler(MessageHandler(filters.Regex(r"^📋 Мои заявки$"), my_submissions))
    app.add_handler(MessageHandler(filters.Regex(r"^ℹ️ Помощь$"), help_cmd))
    app.add_error_handler(error_handler)
    return app


if __name__ == "__main__":
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)

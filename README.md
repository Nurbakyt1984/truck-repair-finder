# Truck Repair Finder

Telegram bot for truck drivers to find nearby repair services using their live location.

## Features

- 📍 Find approved repair services near the user's location
- 🔧 Truck Repair, Tire Shop, Trailer Repair, Mobile Mechanic, Roadside Service, Towing
- ➕ Users can submit new services
- ⏳ Admin approval workflow
- 📋 Users can see their submissions
- 🗺 Google Maps route links
- 🗄 SQLite by default, PostgreSQL supported through `DATABASE_URL`
- 🚂 Ready for Railway

## 1. Create a Telegram bot

Open Telegram and message `@BotFather`.

Create a bot with `/newbot`, then copy the token.

## 2. Local setup

```bash
python -m venv .venv
```

Activate the environment.

Windows:

```bash
.venv\Scripts\activate
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install packages:

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=YOUR_TOKEN
DATABASE_URL=sqlite:///truck_repair.db
ADMIN_IDS=123456789
SEARCH_RADIUS_MILES=100
```

Run:

```bash
python bot.py
```

## 3. Admin commands

Admin Telegram IDs go in `ADMIN_IDS`, separated by commas.

Commands:

```text
/pending
/approve 123
/delete 123
```

If an admin submits a service, it is approved automatically.

## 4. Railway deployment

Create a Railway project from this GitHub repository.

Add environment variables:

```text
TELEGRAM_BOT_TOKEN
ADMIN_IDS
SEARCH_RADIUS_MILES
DATABASE_URL
```

For a real production deployment, add a Railway PostgreSQL database and use its connection URL as `DATABASE_URL`.

The repository includes `railway.toml` and `Procfile`.

## Important

Never upload your real `.env` file or bot token to GitHub.
The `.gitignore` already excludes `.env`.

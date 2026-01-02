"""
Bot de Telegram con Wompi – versión webhook para Render
"""

import os
import csv
import time
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn
import nest_asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# -------------------------------------------------
nest_asyncio.apply()
load_dotenv()

def must(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return val

# ---------------- CONFIG --------------------------
BOT_TOKEN = must("BOT_TOKEN")
WOMPI_CLIENT_ID = must("WOMPI_CLIENT_ID")
WOMPI_CLIENT_SECRET = must("WOMPI_CLIENT_SECRET")
WOMPI_ID_URL = must("WOMPI_ID_URL")
WOMPI_API_BASE = must("WOMPI_API_BASE")
WOMPI_AUDIENCE = os.getenv("WOMPI_AUDIENCE", "wompi_api")
CHANNEL_ID = int(must("CHANNEL_ID"))
WEBHOOK_URL = must("WEBHOOK_URL")
MODE = os.getenv("MODE", "webhook")

EMAILS_NOTIFICACION = os.getenv(
    "EMAILS_NOTIFICACION", "notificaciones@dummy.local"
)

# ---------------- PLANES --------------------------
SUBS = {
    "promo": {"nombre": "Champions (2 días)", "monto": 10.0, "dias": 2},
    "mensual": {"nombre": "Mensual (30 días)", "monto": 30.0, "dias": 30},
}

# ---------------- CSV -----------------------------
class CSVManager:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.headers)

    def append(self, row):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.headers).writerow(row)

    def get_today_rows(self, user_id):
        today = datetime.utcnow().date()
        rows = []
        if not os.path.exists(self.path):
            return rows
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["user_id"] == str(user_id):
                    if datetime.fromisoformat(r["timestamp_utc"]).date() == today:
                        rows.append(r)
        return rows

csv_links = CSVManager(
    "links.csv",
    ["timestamp_utc", "user_id", "chat_id", "username", "referencia", "idEnlace", "urlEnlace", "monto_usd"]
)

# ---------------- WOMPI ---------------------------
class WompiClient:
    def __init__(self):
        self.token = None

    def _token(self):
        if not self.token:
            r = httpx.post(
                WOMPI_ID_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": WOMPI_CLIENT_ID,
                    "client_secret": WOMPI_CLIENT_SECRET,
                    "audience": WOMPI_AUDIENCE,
                },
            )
            r.raise_for_status()
            self.token = r.json()["access_token"]
        return self.token

    def crear_enlace(self, ref, monto, nombre):
        r = httpx.post(
            f"{WOMPI_API_BASE}/EnlacePago",
            headers={"Authorization": f"Bearer {self._token()}"},
            json={
                "identificadorEnlaceComercio": ref,
                "monto": monto,
                "nombreProducto": nombre,
                "configuracion": {"emailsNotificacion": EMAILS_NOTIFICACION},
            },
        )
        r.raise_for_status()
        return r.json()

wompi = WompiClient()

# ---------------- SCHEDULER -----------------------
scheduler = AsyncIOScheduler()

# ---------------- HANDLERS ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💳 Mensual $30", callback_data="tipo_mensual")],
        [InlineKeyboardButton("⚽ Champions $10", callback_data="tipo_promo")],
    ]
    await update.message.reply_text(
        "Bienvenido, elige tu plan:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def seleccionar_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tipo = q.data.split("_")[1]
    context.user_data["tipo"] = tipo

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Compartir número", request_contact=True)]],
        resize_keyboard=True,
    )
    await q.message.reply_text("Comparte tu número:", reply_markup=kb)

async def recibir_contacto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo = context.user_data.get("tipo")
    if not tipo:
        return

    sub = SUBS[tipo]
    ref = f"tg_{update.effective_user.id}_{int(time.time())}"

    data = wompi.crear_enlace(ref, sub["monto"], sub["nombre"])

    csv_links.append({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "username": update.effective_user.username or "sin",
        "referencia": ref,
        "idEnlace": data.get("idEnlace"),
        "urlEnlace": data.get("urlEnlace"),
        "monto_usd": sub["monto"],
    })

    await update.message.reply_text(
        f"💳 Enlace de pago:\n{data.get('urlEnlace')}",
        reply_markup=ReplyKeyboardRemove(),
    )

# ---------------- APP SETUP -----------------------
async def setup_app():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(seleccionar_tipo, pattern="^tipo_"))
    app.add_handler(MessageHandler(filters.CONTACT, recibir_contacto))
    scheduler.start()
    return app

# ---------------- WEBHOOK -------------------------
if MODE == "local":
    application = asyncio.run(setup_app())
    application.run_polling()
else:
    fastapi_app = FastAPI()
    application = asyncio.run(setup_app())

    @fastapi_app.post("/webhook")
    async def webhook(req: Request):
        update = Update.de_json(await req.json(), application.bot)
        await application.process_update(update)
        return {"ok": True}

    @fastapi_app.on_event("startup")
    async def on_startup():
        await application.bot.set_webhook(WEBHOOK_URL)

    if __name__ == "__main__":
        uvicorn.run(
            fastapi_app,
            host="0.0.0.0",
            port=int(os.getenv("PORT", 10000)),
        )

import os, csv
from datetime import datetime, timedelta, timezone
import httpx
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from fastapi import FastAPI, Request
import uvicorn

# -------------------- Inicialización --------------------
load_dotenv()

def must(name):
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return val

BOT_TOKEN = must("BOT_TOKEN")
WOMPI_CLIENT_ID = must("WOMPI_CLIENT_ID")
WOMPI_CLIENT_SECRET = must("WOMPI_CLIENT_SECRET")
WOMPI_AUDIENCE = os.getenv("WOMPI_AUDIENCE", "wompi_api")
WOMPI_ID_URL = must("WOMPI_ID_URL")
WOMPI_API_BASE = must("WOMPI_API_BASE")
CHANNEL_ID = int(must("CHANNEL_ID"))
EMAILS_NOTIFICACION = os.getenv("EMAILS_NOTIFICACION", "notificaciones@dummy.local")
WEBHOOK_URL = must("WEBHOOK_URL")

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/El_Salvador")
except:
    LOCAL_TZ = timezone(timedelta(hours=-6))

# -------------------- Promociones --------------------
CHAMPIONS_ENABLED = True

SUBS = {
    "promo": {"nombre": "Promoción Champions League (2 días)", "monto": 10.00, "dias": 2},
    "mensual": {"nombre": "Suscripción completa (30 días)", "monto": 30.00, "dias": 30},
}

CODIGOS_PROMO = {
    "BRYAN22": 0.10,
}

# -------------------- CSV helpers --------------------
class CSVManager:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers
        if not os.path.isfile(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.headers)

    def append(self, row):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.headers).writerow(row)

csv_links = CSVManager(
    "links.csv",
    ["timestamp_utc","user_id","chat_id","username","referencia","idEnlace","urlEnlace","monto_usd"]
)
csv_valid = CSVManager(
    "validaciones.csv",
    ["timestamp_utc","user_id","referencia","idEnlace","estado"]
)
csv_subs = CSVManager(
    "subs.csv",
    ["user_id","tipo","expiracion_utc","estado"]
)

# -------------------- Cliente Wompi --------------------
class WompiClient:
    def __init__(self):
        self.token = None

    def _get_token(self):
        if not self.token:
            data = {
                "grant_type": "client_credentials",
                "client_id": WOMPI_CLIENT_ID,
                "client_secret": WOMPI_CLIENT_SECRET,
                "audience": WOMPI_AUDIENCE,
            }
            with httpx.Client(timeout=30) as c:
                r = c.post(
                    WOMPI_ID_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                r.raise_for_status()
                self.token = r.json()["access_token"]
        return self.token

    def crear_enlace(self, ref, monto, nombre):
        url = f"{WOMPI_API_BASE}/EnlacePago"
        payload = {
            "identificadorEnlaceComercio": ref,
            "monto": monto,
            "nombreProducto": nombre,
            "configuracion": {"emailsNotificacion": EMAILS_NOTIFICACION},
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    def consultar(self, id_enlace):
        url = f"{WOMPI_API_BASE}/EnlacePago/{id_enlace}"
        with httpx.Client(timeout=30) as c:
            r = c.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            return r.json()

wompi = WompiClient()

# -------------------- Scheduler --------------------
scheduler = AsyncIOScheduler()

class SubManager:
    def __init__(self, app):
        self.app = app

    async def recordar(self, user_id):
        await self.app.bot.send_message(
            user_id,
            "⚠️ Tu suscripción vence en 12 horas. Renueva para evitar suspensión."
        )

    async def expirar(self, user_id):
        await self.app.bot.ban_chat_member(CHANNEL_ID, user_id)
        await self.app.bot.send_message(
            user_id,
            "❌ Tu suscripción expiró. Has sido removido del canal."
        )

    def programar(self, user_id, exp):
        scheduler.add_job(
            self.recordar,
            DateTrigger(run_date=exp - timedelta(hours=12)),
            args=[user_id],
        )
        scheduler.add_job(
            self.expirar,
            DateTrigger(run_date=exp),
            args=[user_id],
        )

# -------------------- Handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    if CHAMPIONS_ENABLED:
        kb.append([InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="tipo_mensual")])
        kb.append([InlineKeyboardButton("⚽ Champions $10 (2 días)", callback_data="tipo_promo")])
    else:
        kb.append([InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="tipo_mensual")])

    await update.message.reply_text(
        "👋 Bienvenido. Selecciona tu plan:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

# -------------------- App Telegram --------------------
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
subm = SubManager(application)

# -------------------- FastAPI --------------------
fastapi_app = FastAPI()

@fastapi_app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@fastapi_app.on_event("startup")
async def startup():
    scheduler.start()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(WEBHOOK_URL)

# -------------------- Ejecutar --------------------
if __name__ == "__main__":
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080)),
    )

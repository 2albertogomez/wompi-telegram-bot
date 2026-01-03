import os
import csv
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

# ======================================================
# INICIALIZACIÓN
# ======================================================
load_dotenv()

def must(name: str) -> str:
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
WEBHOOK_URL = must("WEBHOOK_URL")
EMAILS_NOTIFICACION = os.getenv("EMAILS_NOTIFICACION", "notificaciones@dummy.local")

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/El_Salvador")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=-6))

# ======================================================
# PROMOCIONES
# ======================================================
CHAMPIONS_ENABLED = True

SUBS = {
    "promo": {
        "nombre": "Promoción Champions League (2 días)",
        "monto": 10.00,
        "dias": 2,
    },
    "mensual": {
        "nombre": "Suscripción completa (30 días)",
        "monto": 30.00,
        "dias": 30,
    },
}

CODIGOS_PROMO = {
    "BRYAN22": 0.95,
}

# ======================================================
# CSV HELPERS
# ======================================================
class CSVManager:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers
        if not os.path.isfile(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.headers)

    def append(self, row: dict):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.headers).writerow(row)

csv_links = CSVManager(
    "links.csv",
    [
        "timestamp_utc",
        "user_id",
        "chat_id",
        "username",
        "referencia",
        "idEnlace",
        "urlEnlace",
        "monto_usd",
    ],
)

csv_subs = CSVManager(
    "subs.csv",
    ["user_id", "tipo", "expiracion_utc", "estado"],
)

# ======================================================
# CLIENTE WOMPI
# ======================================================
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

    def crear_enlace(self, referencia, monto, nombre):
        url = f"{WOMPI_API_BASE}/EnlacePago"
        payload = {
            "identificadorEnlaceComercio": referencia,
            "monto": monto,
            "nombreProducto": nombre,
            "configuracion": {
                "emailsNotificacion": EMAILS_NOTIFICACION
            },
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

wompi = WompiClient()

# ======================================================
# UTILIDADES
# ======================================================
def get_user_from_update(update: Update):
    if update.message and update.message.from_user:
        return update.message.from_user
    if update.callback_query and update.callback_query.from_user:
        return update.callback_query.from_user
    return None

# ======================================================
# SCHEDULER
# ======================================================
scheduler = AsyncIOScheduler()

class SubManager:
    def __init__(self, app: Application):
        self.app = app

    async def recordar(self, user_id: int):
        await self.app.bot.send_message(
            user_id,
            "⚠️ Tu suscripción vence en 12 horas. Renueva para evitar suspensión.",
        )

    async def expirar(self, user_id: int):
        await self.app.bot.ban_chat_member(CHANNEL_ID, user_id)
        await self.app.bot.send_message(
            user_id,
            "❌ Tu suscripción expiró. Has sido removido del canal.",
        )

    def programar(self, user_id: int, exp: datetime):
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

# ======================================================
# HANDLERS TELEGRAM
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []

    kb.append(
        [InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="plan_mensual")]
    )

    if CHAMPIONS_ENABLED:
        kb.append(
            [InlineKeyboardButton("⚽ Champions $10 (2 días)", callback_data="plan_promo")]
        )

    await update.message.reply_text(
        "👋 Bienvenido. Selecciona tu plan:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def seleccionar_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plan = query.data.replace("plan_", "")
    context.user_data["plan"] = plan

    await query.message.reply_text(
        "✉️ Si tienes código promocional escríbelo ahora.\n"
        "Si no, escribe *NO*.",
        parse_mode="Markdown",
    )

async def recibir_codigo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().upper()

    descuento = CODIGOS_PROMO.get(texto, 0.0)
    await generar_pago(update, context, descuento)

async def generar_pago(update: Update, context: ContextTypes.DEFAULT_TYPE, descuento=0.0):
    user = get_user_from_update(update)
    if not user:
        return

    plan = context.user_data.get("plan")
    if plan not in SUBS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Plan inválido.",
        )
        return

    sub = SUBS[plan]
    monto = sub["monto"]

    if descuento > 0:
        monto = round(monto * (1 - descuento), 2)

    referencia = f"{plan}_{user.id}_{int(datetime.utcnow().timestamp())}"

    enlace = wompi.crear_enlace(referencia, monto, sub["nombre"])
    url_pago = enlace.get("urlEnlace") or enlace.get("url")

    csv_links.append({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "user_id": user.id,
        "chat_id": update.effective_chat.id,
        "username": user.username or "",
        "referencia": referencia,
        "idEnlace": enlace.get("idEnlace", ""),
        "urlEnlace": url_pago,
        "monto_usd": monto,
    })

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"💳 Para completar tu pago ingresa aquí:\n\n{url_pago}",
    )

# ======================================================
# ERROR HANDLER
# ======================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("ERROR:", context.error)

# ======================================================
# APLICACIÓN TELEGRAM
# ======================================================
application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(seleccionar_plan, pattern="^plan_"))
application.add_handler(CommandHandler("codigo", recibir_codigo))
application.add_error_handler(error_handler)

subm = SubManager(application)

# ======================================================
# FASTAPI
# ======================================================
fastapi_app = FastAPI()

@fastapi_app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@fastapi_app.on_event("startup")
async def on_startup():
    scheduler.start()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(WEBHOOK_URL)

# ======================================================
# EJECUCIÓN
# ======================================================
if __name__ == "__main__":
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
    )

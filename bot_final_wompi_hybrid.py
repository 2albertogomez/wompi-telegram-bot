import os
import csv
import uuid
import logging
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from fastapi import FastAPI, Request
import uvicorn

# -------------------------------------------------
# LOGGING
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
log = logging.getLogger("wompi-bot")

# -------------------------------------------------
# ENV
# -------------------------------------------------
load_dotenv()

def must(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return v

BOT_TOKEN = must("BOT_TOKEN")
WOMPI_CLIENT_ID = must("WOMPI_CLIENT_ID")
WOMPI_CLIENT_SECRET = must("WOMPI_CLIENT_SECRET")
WOMPI_AUDIENCE = os.getenv("WOMPI_AUDIENCE", "wompi_api")
WOMPI_ID_URL = must("WOMPI_ID_URL")
WOMPI_API_BASE = must("WOMPI_API_BASE")
WEBHOOK_URL = must("WEBHOOK_URL")
CHANNEL_ID = int(must("CHANNEL_ID"))
EMAILS_NOTIFICACION = os.getenv("EMAILS_NOTIFICACION", "notificaciones@dummy.local")

PORT = int(os.getenv("PORT", 10000))

# -------------------------------------------------
# TIMEZONE
# -------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/El_Salvador")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=-6))

# -------------------------------------------------
# PLANES
# -------------------------------------------------
SUBS = {
    "mensual": {
        "nombre": "Suscripción Mensual (30 días)",
        "monto": 30.00,
        "dias": 30,
    },
    "promo": {
        "nombre": "Champions League (2 días)",
        "monto": 10.00,
        "dias": 2,
    },
}

# PROMOCIONES (solo mensual)
CODIGOS_PROMO = {
    "BRYAN22": 0.10,
    "SOCIO50": 0.50,
}

# -------------------------------------------------
# CSV (simple, local)
# -------------------------------------------------
class CSVManager:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(headers)

    def append(self, row: dict):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.headers)
            writer.writerow(row)

csv_links = CSVManager(
    "links.csv",
    ["timestamp_utc", "user_id", "plan", "referencia", "id_enlace", "url", "monto"],
)

# -------------------------------------------------
# WOMPI CLIENT
# -------------------------------------------------
class WompiClient:
    def __init__(self):
        self._token = None

    def _get_token(self):
        if self._token:
            return self._token

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
            self._token = r.json()["access_token"]

        return self._token

    def crear_enlace(self, referencia, monto, nombre):
        payload = {
            "identificadorEnlaceComercio": referencia,
            "monto": monto,
            "nombreProducto": nombre,
            "configuracion": {
                "emailsNotificacion": EMAILS_NOTIFICACION,
            },
        }

        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{WOMPI_API_BASE}/EnlacePago",
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            return r.json()

wompi = WompiClient()

# -------------------------------------------------
# SCHEDULER
# -------------------------------------------------
scheduler = AsyncIOScheduler()

# -------------------------------------------------
# TELEGRAM HANDLERS
# -------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="plan_mensual")],
        [InlineKeyboardButton("⚽ Champions $10 (2 días)", callback_data="plan_promo")],
    ]

    await update.message.reply_text(
        "👋 Bienvenido\n\nSelecciona tu plan:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def seleccionar_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    plan_key = q.data.replace("plan_", "")
    plan = SUBS.get(plan_key)

    if not plan:
        await q.message.reply_text("❌ Plan inválido.")
        return

    context.user_data.clear()
    context.user_data["plan"] = plan_key

    if plan_key == "mensual":
        await q.message.reply_text(
            "💡 ¿Tienes un código de promoción?\n"
            "Escríbelo ahora o escribe *NO* para continuar sin descuento.",
            parse_mode="Markdown",
        )
    else:
        await generar_link(q.message, context, descuento=0.0)

async def codigo_promocional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().upper()
    plan_key = context.user_data.get("plan")

    if not plan_key:
        return

    plan = SUBS[plan_key]

    descuento = 0.0
    if texto != "NO":
        descuento = CODIGOS_PROMO.get(texto, 0.0)

    await generar_link(update.message, context, descuento)

async def generar_link(message, context, descuento: float):
    plan_key = context.user_data.get("plan")
    plan = SUBS[plan_key]

    monto_final = round(plan["monto"] * (1 - descuento), 2)

    referencia = f"{message.chat.id}-{uuid.uuid4().hex[:8]}"
    enlace = wompi.crear_enlace(
        referencia,
        monto_final,
        plan["nombre"],
    )

    csv_links.append({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "user_id": message.chat.id,
        "plan": plan_key,
        "referencia": referencia,
        "id_enlace": enlace.get("idEnlace"),
        "url": enlace.get("urlEnlace"),
        "monto": monto_final,
    })

    txt_desc = (
        f"🎉 Descuento aplicado: {int(descuento*100)}%\n"
        if descuento > 0 else ""
    )

    await message.reply_text(
        txt_desc +
        f"💳 *Plan:* {plan['nombre']}\n"
        f"💵 *Monto a pagar:* ${monto_final}\n\n"
        f"👉 *Paga aquí:*\n{enlace['urlEnlace']}",
        parse_mode="Markdown",
    )

    context.user_data.clear()

# -------------------------------------------------
# TELEGRAM APPLICATION
# -------------------------------------------------
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(seleccionar_plan))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, codigo_promocional))

# -------------------------------------------------
# FASTAPI
# -------------------------------------------------
fastapi_app = FastAPI()

@fastapi_app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@fastapi_app.on_event("startup")
async def on_startup():
    log.info("Inicializando Telegram Application...")
    await application.initialize()
    await application.start()

    scheduler.start()

    log.info("Configurando webhook...")
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(WEBHOOK_URL)

@fastapi_app.on_event("shutdown")
async def on_shutdown():
    log.info("Cerrando aplicación...")
    scheduler.shutdown(wait=False)
    await application.stop()
    await application.shutdown()

# -------------------------------------------------
# RUN
# -------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=PORT,
    )

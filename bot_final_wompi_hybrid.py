# =========================
# BOT TELEGRAM + WOMPI
# Render / FastAPI / Webhook
# =========================

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
    ContextTypes,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from fastapi import FastAPI, Request
import uvicorn

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("wompi-bot")

# ---------------- ENV ----------------
load_dotenv()

def must(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return val

BOT_TOKEN = must("BOT_TOKEN")
WOMPI_CLIENT_ID = must("WOMPI_CLIENT_ID")
WOMPI_CLIENT_SECRET = must("WOMPI_CLIENT_SECRET")
WOMPI_ID_URL = must("WOMPI_ID_URL")
WOMPI_API_BASE = must("WOMPI_API_BASE")
WEBHOOK_URL = must("WEBHOOK_URL")
CHANNEL_ID = int(must("CHANNEL_ID"))

EMAILS_NOTIFICACION = os.getenv("EMAILS_NOTIFICACION", "notificaciones@dummy.local")
WOMPI_AUDIENCE = os.getenv("WOMPI_AUDIENCE", "wompi_api")

# ---------------- TIMEZONE ----------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/El_Salvador")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=-6))

# ---------------- PROMOCIONES ----------------
CHAMPIONS_ENABLED = True

SUBS = {
    "mensual": {
        "nombre": "Suscripción mensual (30 días)",
        "monto": 30.00,
        "dias": 30,
    },
    "promo": {
        "nombre": "Promoción Champions (2 días)",
        "monto": 10.00,
        "dias": 2,
    },
}

CODIGOS_PROMO = {
    "BRYAN22": 0.10,  # 10%
}

# ---------------- CSV ----------------
class CSVManager:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers
        if not os.path.isfile(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=headers).writeheader()

    def append(self, row: dict):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.headers).writerow(row)

csv_links = CSVManager(
    "links.csv",
    ["timestamp_utc", "user_id", "tipo", "referencia", "id_enlace", "url", "monto"]
)

csv_subs = CSVManager(
    "subs.csv",
    ["user_id", "tipo", "expiracion_utc", "estado"]
)

# ---------------- WOMPI ----------------
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
                timeout=30,
            )
            r.raise_for_status()
            self.token = r.json()["access_token"]
        return self.token

    def crear_enlace(self, referencia, monto, nombre):
        r = httpx.post(
            f"{WOMPI_API_BASE}/EnlacePago",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            json={
                "identificadorEnlaceComercio": referencia,
                "monto": monto,
                "nombreProducto": nombre,
                "configuracion": {"emailsNotificacion": EMAILS_NOTIFICACION},
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def consultar(self, id_enlace):
        r = httpx.get(
            f"{WOMPI_API_BASE}/EnlacePago/{id_enlace}",
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

wompi = WompiClient()

# ---------------- SCHEDULER ----------------
scheduler = AsyncIOScheduler()

# ---------------- TELEGRAM ----------------
application = Application.builder().token(BOT_TOKEN).build()

# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="plan_mensual")]
    ]
    if CHAMPIONS_ENABLED:
        kb.append(
            [InlineKeyboardButton("⚽ Champions $10 (2 días)", callback_data="plan_promo")]
        )

    await update.message.reply_text(
        "👋 Bienvenido\nSelecciona un plan:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def seleccionar_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tipo = query.data.replace("plan_", "")
    context.user_data["plan"] = tipo

    await query.message.reply_text(
        "¿Tienes un código promocional?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sí", callback_data="promo_si")],
            [InlineKeyboardButton("❌ No", callback_data="promo_no")],
        ]),
    )

async def promo_respuesta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "promo_si":
        await query.message.reply_text("Escribe tu código promocional:")
        context.user_data["esperando_codigo"] = True
    else:
        await crear_pago(update, context)

async def texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("esperando_codigo"):
        code = update.message.text.strip().upper()
        context.user_data["codigo"] = code
        context.user_data["esperando_codigo"] = False
        await crear_pago(update, context)

async def crear_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = context.user_data["plan"]
    sub = SUBS[plan]

    descuento = 0
    code = context.user_data.get("codigo")
    if code and code in CODIGOS_PROMO:
        descuento = CODIGOS_PROMO[code]

    monto_final = round(sub["monto"] * (1 - descuento), 2)
    ref = f"{plan}-{uuid.uuid4().hex[:8]}"

    data = wompi.crear_enlace(ref, monto_final, sub["nombre"])

    csv_links.append({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "user_id": update.effective_user.id,
        "tipo": plan,
        "referencia": ref,
        "id_enlace": data["idEnlace"],
        "url": data["urlEnlace"],
        "monto": monto_final,
    })

    context.user_data["id_enlace"] = data["idEnlace"]
    context.user_data["dias"] = sub["dias"]

    await update.effective_chat.send_message(
        f"💳 Monto a pagar: ${monto_final}\n\n👉 {data['urlEnlace']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Ya pagué", callback_data="ya_pague")]
        ]),
    )

async def ya_pague(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    info = wompi.consultar(context.user_data["id_enlace"])
    estado = info.get("estado")

    if estado != "PAGADO":
        await query.message.reply_text("⏳ El pago aún no se refleja. Intenta luego.")
        return

    exp = datetime.now(tz=LOCAL_TZ) + timedelta(days=context.user_data["dias"])
    csv_subs.append({
        "user_id": query.from_user.id,
        "tipo": context.user_data["plan"],
        "expiracion_utc": exp.astimezone(timezone.utc).isoformat(),
        "estado": "activo",
    })

    await application.bot.unban_chat_member(CHANNEL_ID, query.from_user.id)
    await application.bot.send_message(
        query.from_user.id,
        "✅ Pago confirmado. Acceso activado."
    )

# ---------------- REGISTRO ----------------
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(seleccionar_plan, pattern="^plan_"))
application.add_handler(CallbackQueryHandler(promo_respuesta, pattern="^promo_"))
application.add_handler(CallbackQueryHandler(ya_pague, pattern="^ya_pague$"))
application.add_handler(CommandHandler("cancel", start))
application.add_handler(
    telegram.ext.MessageHandler(telegram.ext.filters.TEXT & ~telegram.ext.filters.COMMAND, texto)
)

# ---------------- FASTAPI ----------------
fastapi_app = FastAPI()

@fastapi_app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@fastapi_app.on_event("startup")
async def startup():
    logger.info("Inicializando Telegram Application...")
    await application.initialize()
    await application.start()
    scheduler.start()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(WEBHOOK_URL)
    logger.info("Webhook listo")

@fastapi_app.on_event("shutdown")
async def shutdown():
    logger.info("Cerrando aplicación...")
    scheduler.shutdown()
    await application.stop()
    await application.shutdown()

# ---------------- MAIN ----------------
if __name__ == "__main__":
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )

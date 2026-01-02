"""
Bot de Telegram para promociones con Wompi (Versión híbrida: local o Render).
Incluye: /start, selección de promoción, validación de pago, recordatorios, baneo automático.
Incluye sistema de códigos de referidos aplicable al plan mensual.
"""

import os, csv, time
from datetime import datetime, timedelta, timezone
import asyncio
import httpx
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from fastapi import FastAPI, Request
import uvicorn
import nest_asyncio

# --------------------------------------------------
nest_asyncio.apply()
load_dotenv()

def must(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return val

# ---------------- CONFIG ---------------------------
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

# --------------------------------------------------
SUBS = {
    "promo": {"nombre": "Promoción Champions (2 días)", "monto": 10.00, "dias": 2},
    "mensual": {"nombre": "Suscripción mensual (30 días)", "monto": 30.00, "dias": 30},
}

CODIGOS_PROMO = {
    "BRYAN22": 0.10,
}

# ---------------- CSV ------------------------------
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

    def get_today_rows(self, user_id):
        if not os.path.isfile(self.path):
            return []
        today = datetime.utcnow().date()
        out = []
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["user_id"] == str(user_id):
                    dt = datetime.fromisoformat(r["timestamp_utc"])
                    if dt.date() == today:
                        out.append(r)
        return out

csv_links = CSVManager(
    "links.csv",
    ["timestamp_utc","user_id","chat_id","username","referencia","idEnlace","urlEnlace","monto_usd"]
)
csv_subs = CSVManager(
    "subs.csv",
    ["user_id","tipo","expiracion_utc","estado"]
)

# ---------------- WOMPI ----------------------------
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
            r = httpx.post(WOMPI_ID_URL, data=data)
            r.raise_for_status()
            self.token = r.json()["access_token"]
        return self.token

    def crear_enlace(self, ref, monto, nombre):
        payload = {
            "identificadorEnlaceComercio": ref,
            "monto": monto,
            "nombreProducto": nombre,
            "configuracion": {"emailsNotificacion": EMAILS_NOTIFICACION},
        }
        r = httpx.post(
            f"{WOMPI_API_BASE}/EnlacePago",
            headers={"Authorization": f"Bearer {self._get_token()}"},
            json=payload
        )
        r.raise_for_status()
        return r.json()

    def consultar(self, id_enlace):
        r = httpx.get(
            f"{WOMPI_API_BASE}/EnlacePago/{id_enlace}",
            headers={"Authorization": f"Bearer {self._get_token()}"}
        )
        r.raise_for_status()
        return r.json()

wompi = WompiClient()

# ---------------- SUBS -----------------------------
scheduler = AsyncIOScheduler()

class SubManager:
    def __init__(self, app):
        self.app = app

    async def expirar(self, user_id):
        await self.app.bot.ban_chat_member(CHANNEL_ID, user_id)

    def programar(self, user_id, exp):
        scheduler.add_job(
            self.expirar,
            DateTrigger(run_date=exp),
            args=[user_id]
        )

# ---------------- BOT HANDLERS ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💳 Mensual $30", callback_data="tipo_mensual")],
        [InlineKeyboardButton("⚽ Champions $10", callback_data="tipo_promo")]
    ]
    await update.message.reply_text(
        "👋 Bienvenido, selecciona tu plan:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def seleccionar_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tipo = q.data.split("_")[1]
    context.user_data["tipo"] = tipo

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Compartir número", request_contact=True)]],
        resize_keyboard=True
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
        "monto_usd": sub["monto"]
    })

    await update.message.reply_text(
        f"💳 Enlace de pago:\n{data.get('urlEnlace')}",
        reply_markup=ReplyKeyboardRemove()
    )

async def validar_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = csv_links.get_today_rows(update.effective_user.id)
    if not rows:
        await update.message.reply_text("No hay pagos.")
        return

    reg = rows[-1]
    data = wompi.consultar(reg["idEnlace"])

    if data.get("estado") == "APROBADA":
        exp = datetime.utcnow() + timedelta(days=SUBS["mensual"]["dias"])
        csv_subs.append({
            "user_id": update.effective_user.id,
            "tipo": "mensual",
            "expiracion_utc": exp.isoformat(),
            "estado": "activa"
        })
        await context.bot.unban_chat_member(CHANNEL_ID, update.effective_user.id)
        subm.programar(update.effective_user.id, exp)
        await update.message.reply_text("✅ Pago aprobado.")

# ---------------- APP SETUP ------------------------
async def setup_app():
    global subm
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("validar_pago", validar_pago))
    app.add_handler(CallbackQueryHandler(seleccionar_tipo, pattern="^tipo_"))
    app.add_handler(MessageHandler(filters.CONTACT, recibir_contacto))
    scheduler.start()
    subm = SubManager(app)
    return app

# ---------------- RENDER WEBHOOK -------------------
if MODE == "local":
    application = asyncio.run(setu

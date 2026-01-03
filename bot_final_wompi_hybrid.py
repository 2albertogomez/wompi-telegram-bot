import os, csv, logging
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

def must(name):
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return v

BOT_TOKEN = must("BOT_TOKEN")
WOMPI_CLIENT_ID = must("WOMPI_CLIENT_ID")
WOMPI_CLIENT_SECRET = must("WOMPI_CLIENT_SECRET")
WOMPI_ID_URL = must("WOMPI_ID_URL")
WOMPI_API_BASE = must("WOMPI_API_BASE")
WEBHOOK_URL = must("WEBHOOK_URL")
CHANNEL_ID = int(must("CHANNEL_ID"))
EMAILS_NOTIFICACION = os.getenv("EMAILS_NOTIFICACION", "notificaciones@dummy.local")

# -------------------------------------------------
# TIMEZONE
# -------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/El_Salvador")
except:
    LOCAL_TZ = timezone(timedelta(hours=-6))

# -------------------------------------------------
# PLANES
# -------------------------------------------------
SUBS = {
    "mensual": {"nombre": "Suscripción Mensual (30 días)", "monto": 30.00, "dias": 30},
    "promo": {"nombre": "Promoción Champions (2 días)", "monto": 10.00, "dias": 2},
}

# -------------------------------------------------
# CÓDIGOS PROMOCIONALES
# SOLO APLICAN AL PLAN MENSUAL
# -------------------------------------------------
CODIGOS_PROMO = {
    "BRYAN22": 0.99,  # 10%
}

# -------------------------------------------------
# CSV
# -------------------------------------------------
def csv_append(path, headers, row):
    exists = os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if not exists:
            w.writeheader()
        w.writerow(row)

# -------------------------------------------------
# WOMPI
# -------------------------------------------------
class WompiClient:
    def __init__(self):
        self.token = None

    def token_ok(self):
        if self.token:
            return self.token

        data = {
            "grant_type": "client_credentials",
            "client_id": WOMPI_CLIENT_ID,
            "client_secret": WOMPI_CLIENT_SECRET,
            "audience": "wompi_api",
        }

        r = httpx.post(WOMPI_ID_URL, data=data, timeout=30)
        r.raise_for_status()
        self.token = r.json()["access_token"]
        return self.token

    def crear_enlace(self, ref, monto, nombre):
        r = httpx.post(
            f"{WOMPI_API_BASE}/EnlacePago",
            headers={
                "Authorization": f"Bearer {self.token_ok()}",
                "Content-Type": "application/json",
            },
            json={
                "identificadorEnlaceComercio": ref,
                "monto": monto,
                "nombreProducto": nombre,
                "configuracion": {
                    "emailsNotificacion": EMAILS_NOTIFICACION
                },
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def consultar(self, id_enlace):
        r = httpx.get(
            f"{WOMPI_API_BASE}/EnlacePago/{id_enlace}",
            headers={"Authorization": f"Bearer {self.token_ok()}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

wompi = WompiClient()

# -------------------------------------------------
# SCHEDULER
# -------------------------------------------------
scheduler = AsyncIOScheduler()

async def recordar(app, user_id):
    await app.bot.send_message(
        user_id,
        "⚠️ Tu suscripción vence en 12 horas. Renueva para no perder acceso."
    )

async def expirar(app, user_id):
    await app.bot.ban_chat_member(CHANNEL_ID, user_id)
    await app.bot.send_message(
        user_id,
        "❌ Tu suscripción expiró. Has sido removido del canal."
    )

# -------------------------------------------------
# TELEGRAM APP
# -------------------------------------------------
application = Application.builder().token(BOT_TOKEN).build()

# -------------------------------------------------
# HANDLERS
# -------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="plan_mensual")],
        [InlineKeyboardButton("⚽ Promo $10 (2 días)", callback_data="plan_promo")],
    ]
    await update.message.reply_text(
        "👋 Bienvenido\n\nSelecciona tu plan:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def elegir_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    plan = q.data.replace("plan_", "")
    context.user_data.clear()
    context.user_data["plan"] = plan

    if plan == "mensual":
        kb = [
            [InlineKeyboardButton("✅ Sí", callback_data="codigo_si")],
            [InlineKeyboardButton("❌ No", callback_data="codigo_no")],
        ]
        await q.message.reply_text(
            "¿Tienes código promocional?",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await generar_pago(q, context, descuento=0)

async def sin_codigo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await generar_pago(q, context, descuento=0)

async def pedir_codigo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["esperando_codigo"] = True
    await q.message.reply_text("✍️ Escribe tu código promocional:")

async def recibir_codigo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("esperando_codigo"):
        return

    codigo = update.message.text.strip().upper()
    context.user_data["esperando_codigo"] = False

    descuento = CODIGOS_PROMO.get(codigo)
    if not descuento:
        await update.message.reply_text("❌ Código inválido.")
        descuento = 0
    else:
        await update.message.reply_text(
            f"✅ Código aplicado: {int(descuento*100)}% descuento"
        )

    await generar_pago(update, context, descuento)

async def generar_pago(source, context, descuento):
    plan = context.user_data["plan"]
    sub = SUBS[plan]

    monto = round(sub["monto"] * (1 - descuento), 2)
    ref = f"{plan}_{source.from_user.id}_{int(datetime.now().timestamp())}"

    data = wompi.crear_enlace(ref, monto, sub["nombre"])

    csv_append(
        "links.csv",
        ["timestamp","user","plan","monto","id","url"],
        {
            "timestamp": datetime.utcnow().isoformat(),
            "user": source.from_user.id,
            "plan": plan,
            "monto": monto,
            "id": data["idEnlace"],
            "url": data["urlEnlace"],
        },
    )

    await source.message.reply_text(
        f"💳 Total a pagar: ${monto:.2f}\n\n{data['urlEnlace']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Ya pagué", callback_data=f"verificar_{data['idEnlace']}")]
        ])
    )

async def verificar_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    id_enlace = q.data.replace("verificar_", "")
    data = wompi.consultar(id_enlace)

    if data.get("estado") != "PAGADO":
        await q.message.reply_text("⏳ Pago aún no confirmado.")
        return

    plan = context.user_data["plan"]
    dias = SUBS[plan]["dias"]
    user_id = q.from_user.id

    await application.bot.unban_chat_member(CHANNEL_ID, user_id)
    await application.bot.send_message(user_id, "✅ Pago confirmado. Acceso habilitado.")

    exp = datetime.now(LOCAL_TZ) + timedelta(days=dias)

    scheduler.add_job(recordar, DateTrigger(exp - timedelta(hours=12)), args=[application, user_id])
    scheduler.add_job(expirar, DateTrigger(exp), args=[application, user_id])

# -------------------------------------------------
# REGISTRO HANDLERS
# -------------------------------------------------
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(elegir_plan, pattern="^plan_"))
application.add_handler(CallbackQueryHandler(sin_codigo, pattern="^codigo_no$"))
application.add_handler(CallbackQueryHandler(pedir_codigo, pattern="^codigo_si$"))
application.add_handler(CallbackQueryHandler(verificar_pago, pattern="^verificar_"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_codigo))

# -------------------------------------------------
# FASTAPI
# -------------------------------------------------
app = FastAPI()

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def startup():
    log.info("Inicializando Telegram Application...")
    await application.initialize()
    await application.start()
    scheduler.start()
    await application.bot.set_webhook(WEBHOOK_URL)

@app.on_event("shutdown")
async def shutdown():
    log.info("Cerrando aplicación...")
    scheduler.shutdown()
    await application.stop()

# -------------------------------------------------
# RUN
# -------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

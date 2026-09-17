from fastapi import FastAPI, APIRouter, Request, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from telegram import Update

from near_client import NearBridgeClient
import bot as botmod

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# httpx logs full request URLs which include the bot token; keep it out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', 'hook')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL')

near = NearBridgeClient()
state = {"application": None, "bot_info": {}}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if TELEGRAM_TOKEN:
        try:
            application = botmod.create_application(TELEGRAM_TOKEN, db, near)
            await application.initialize()
            await application.start()
            me = await application.bot.get_me()
            state["application"] = application
            state["bot_info"] = {"username": me.username, "name": me.first_name}
            from telegram import BotCommand
            await application.bot.set_my_commands([
                BotCommand("bridge", "Start a new Base to Starknet bridge"),
                BotCommand("invoice", "Create a client payment invoice"),
                BotCommand("addresses", "Manage your saved addresses"),
                BotCommand("history", "See your recent bridges"),
                BotCommand("privacy", "How your privacy is protected"),
                BotCommand("forget", "Wipe all your saved data"),
                BotCommand("start", "Show the main menu"),
            ])
            if PUBLIC_BASE_URL:
                url = f"{PUBLIC_BASE_URL}/api/telegram/webhook/{WEBHOOK_SECRET}"
                await application.bot.set_webhook(
                    url=url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
                )
                logger.info("Telegram webhook set to %s", url)
            botmod.start_poller(application)
            logger.info("Bot @%s started", me.username)
        except Exception:
            logger.exception("Failed to start Telegram bot")
    yield
    app_ = state.get("application")
    if app_:
        try:
            await botmod.stop_poller()
            await app_.stop()
            await app_.shutdown()
        except Exception:
            logger.exception("Error during bot shutdown")
    client.close()


app = FastAPI(lifespan=lifespan)
api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"message": "Base -> Starknet auto-bridge bot is running"}


@api_router.get("/bot-info")
async def bot_info():
    info = state.get("bot_info", {})
    username = info.get("username")
    return {
        "username": username,
        "name": info.get("name"),
        "link": f"https://t.me/{username}" if username else None,
    }


@api_router.get("/stats")
async def stats():
    total = await db.swaps.count_documents({})
    completed = await db.swaps.count_documents({"status": "SUCCESS"})
    return {"total_swaps": total, "completed": completed}


@api_router.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    application = state.get("application")
    if not application:
        raise HTTPException(status_code=503, detail="bot not ready")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

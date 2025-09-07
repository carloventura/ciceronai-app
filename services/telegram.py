from telegram import Bot
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = Bot(token=TELEGRAM_TOKEN)

async def send_message(chat_id, text):
    bot.send_message(chat_id=chat_id, text=text)

async def get_user_location(chat_id):
    # Placeholder: implementa logica per richiedere posizione
    return None

async def get_photo_file(photo):
    # Placeholder: scarica la foto dal server Telegram
    return None

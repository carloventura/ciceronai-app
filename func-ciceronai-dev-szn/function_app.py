import logging
import os

import azure.functions as func
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

app = func.FunctionApp()


@app.route(
    route="ChatMessage",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=[func.HttpMethod.POST],
)
async def ChatMessage(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Python HTTP trigger function processed a request.")
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Error", status_code=400)

    async with Bot(os.environ["TELEGRAM_TOKEN"]) as bot:
        if "callback_query" in body:
            callback_query = body["callback_query"]
            await bot.answer_callback_query(callback_query["id"])
            chat_id = callback_query["message"]["chat"]["id"]
            callback_data = callback_query.get("data")

            if callback_data == "new_session":
                await bot.send_message(
                    chat_id,
                    "La creazione di una nuova sessione non è ancora disponibile.",
                )
            elif callback_data == "enter_session_code":
                await bot.send_message(
                    chat_id,
                    "L'inserimento e la verifica del codice di sessione non sono ancora disponibili.",
                )

            return func.HttpResponse("OK", status_code=200)

        if "message" not in body or "chat" not in body["message"]:
            return func.HttpResponse("Unsupported update", status_code=400)

        message = body["message"]
        chat_id = message["chat"]["id"]

        if message.get("text") == "/start":
            inline_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Avvia nuova sessione", callback_data="new_session")],
                [InlineKeyboardButton("Ho un codice di sessione", callback_data="enter_session_code")],
            ])
            await bot.send_message(
                chat_id,
                "Vuoi avviare una nuova sessione o hai già un codice di sessione?",
                reply_markup=inline_keyboard,
            )

    return func.HttpResponse("OK", status_code=200)
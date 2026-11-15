import logging
import os
import random
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import azure.functions as func
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from fantasy_names import ADJECTIVES, NOUNS

app = func.FunctionApp()


def generate_session_name() -> str:
    return f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"


def get_retention_days() -> int:
    try:
        return max(1, int(os.getenv("CHAT_RETENTION_DAYS", "1")))
    except ValueError:
        logging.warning("Invalid CHAT_RETENTION_DAYS; using one day.")
        return 1


@lru_cache(maxsize=1)
def get_table_client() -> TableClient:
    account_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
    table_name = os.getenv("CHAT_SESSIONS_TABLE_NAME", "ChatSessions")

    if not account_name or not account_key:
        raise RuntimeError("Missing Azure Table Storage configuration.")

    service = TableServiceClient(
        endpoint=f"https://{account_name}.table.core.windows.net",
        credential=AzureNamedKeyCredential(account_name, account_key),
    )
    service.create_table_if_not_exists(table_name)
    return service.get_table_client(table_name)


def get_session_by_name(session_name: str) -> dict | None:
    try:
        return get_table_client().get_entity(
            partition_key=session_name,
            row_key="session",
        )
    except ResourceNotFoundError:
        return None


def save_session(chat_id: int, status: str, session_name: str | None = None) -> str:
    table_client = get_table_client()
    if not session_name:
        session_name = generate_session_name()
        while get_session_by_name(session_name) is not None:
            session_name = generate_session_name()

    now = datetime.now(timezone.utc).isoformat()
    table_client.upsert_entity({
        "PartitionKey": session_name,
        "RowKey": "session",
        "chatId": str(chat_id),
        "sessionCode": session_name,
        "displayName": session_name,
        "status": status,
        "createdAt": now,
        "lastUpdated": now,
    })
    return session_name


@app.schedule(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False)
def cleanup_expired_sessions(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.info("The cleanup timer is past due.")

    cutoff = datetime.now(timezone.utc) - timedelta(days=get_retention_days())
    table_client = get_table_client()

    for entity in table_client.list_entities():
        last_updated = entity.get("lastUpdated")
        if not last_updated:
            continue

        try:
            last_updated_dt = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
        except ValueError:
            logging.warning("Skipping session with invalid lastUpdated value.")
            continue

        if last_updated_dt.tzinfo is None:
            last_updated_dt = last_updated_dt.replace(tzinfo=timezone.utc)

        if last_updated_dt < cutoff:
            table_client.delete_entity(entity["PartitionKey"], entity["RowKey"])
            logging.info("Deleted expired session %s", entity.get("sessionCode"))


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
                session_name = save_session(chat_id, "active")
                await bot.send_message(
                    chat_id,
                    f"Benvenuto! La tua sessione è pronta.\n\nNome della chat: {session_name}\nCodice sessione: {session_name}\n\nUsa questo nome per recuperarla.",
                )
            elif callback_data == "enter_session_code":
                await bot.send_message(
                    chat_id,
                    "Scrivi il nome della tua sessione per recuperarla.",
                )

            return func.HttpResponse("OK", status_code=200)

        if "message" not in body or "chat" not in body["message"]:
            return func.HttpResponse("Unsupported update", status_code=400)

        message = body["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()

        if text == "/start":
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

        if text:
            session_code = text.lower()
            valid_code = (
                session_code[0].isalnum()
                and session_code[-1].isalnum()
                and all(
                    character.isascii() and (character.isalnum() or character == "-")
                    for character in session_code
                )
            )
            session = get_session_by_name(session_code) if valid_code else None
            if session:
                now = datetime.now(timezone.utc).isoformat()
                get_table_client().upsert_entity({
                    "PartitionKey": session["PartitionKey"],
                    "RowKey": session["RowKey"],
                    "chatId": str(chat_id),
                    "sessionCode": session["sessionCode"],
                    "displayName": session["displayName"],
                    "status": "active",
                    "createdAt": session.get("createdAt", now),
                    "lastUpdated": now,
                })
                await bot.send_message(
                    chat_id,
                    f"Sessione recuperata: {session['displayName']}",
                )
            else:
                await bot.send_message(
                    chat_id,
                    "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                )

    return func.HttpResponse("OK", status_code=200)
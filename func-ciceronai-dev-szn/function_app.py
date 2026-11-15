import logging
import os
import random
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, TableSharedKeyCredential
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from fantasy_names import ADJECTIVES, NOUNS

app = func.FunctionApp()


def generate_session_name() -> str:
    adjective = random.choice(ADJECTIVES)
    noun = random.choice(NOUNS)
    return f"{adjective}-{noun}"


def get_retention_days() -> int:
    try:
        return max(1, int(os.getenv("CHAT_RETENTION_DAYS", "1")))
    except ValueError:
        return 1


def get_table_client() -> TableClient:
    account_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
    table_name = os.getenv("CHAT_SESSIONS_TABLE_NAME", "ChatSessions")

    if not account_name or not account_key:
        raise RuntimeError("Missing Azure Storage configuration.")

    service = TableServiceClient(
        endpoint=f"https://{account_name}.table.core.windows.net",
        credential=TableSharedKeyCredential(account_name, account_key),
    )
    table_client = service.get_table_client(table_name)
    table_client.create_table_if_not_exists()
    return table_client


def get_session_entity(chat_id: int) -> dict | None:
    table_client = get_table_client()
    try:
        return table_client.get_entity(partition_key=str(chat_id), row_key="session")
    except ResourceNotFoundError:
        return None


def get_session_by_name(session_name: str) -> dict | None:
    normalized = session_name.strip().lower()
    table_client = get_table_client()
    for entity in table_client.list_entities():
        if str(entity.get("sessionName", "")).lower() == normalized:
            return entity
    return None


def save_session(chat_id: int, status: str, session_name: str | None = None) -> str:
    table_client = get_table_client()
    existing = get_session_entity(chat_id)
    now = datetime.now(timezone.utc).isoformat()

    if existing and existing.get("sessionName"):
        session_name = existing["sessionName"]
    elif not session_name:
        session_name = generate_session_name()
        while get_session_by_name(session_name) is not None:
            session_name = generate_session_name()

    entity = {
        "PartitionKey": str(chat_id),
        "RowKey": "session",
        "chatId": str(chat_id),
        "sessionName": session_name,
        "status": status,
        "lastUpdated": now,
        "createdAt": existing.get("createdAt", now) if existing else now,
    }

    table_client.upsert_entity(entity)
    return session_name


@app.schedule(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False)
def cleanup_expired_sessions(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.info("The timer is past due.")

    try:
        table_client = get_table_client()
    except RuntimeError as exc:
        logging.exception("Storage configuration missing: %s", exc)
        return

    retention_days = get_retention_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    for entity in table_client.list_entities():
        last_updated = entity.get("lastUpdated")
        if not last_updated:
            continue

        try:
            last_updated_dt = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
        except ValueError:
            continue

        if last_updated_dt.tzinfo is None:
            last_updated_dt = last_updated_dt.replace(tzinfo=timezone.utc)

        if last_updated_dt < cutoff:
            table_client.delete_entity(entity["PartitionKey"], entity["RowKey"])
            logging.info("Deleted expired session for chat %s", entity.get("chatId"))


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
                session_name = save_session(chat_id, "new_session")
                await bot.send_message(
                    chat_id,
                    f"Benvenuto! La tua sessione è pronta. \n\nNome della chat: {session_name}\n\nUsa questo nome per recuperare la sessione in futuro.",
                )
            elif callback_data == "enter_session_code":
                await bot.send_message(
                    chat_id,
                    "Scrivi il nome della tua sessione (esempio: moon-vale) per recuperarla.",
                )
            return func.HttpResponse("OK", status_code=200)

        if "message" not in body or "chat" not in body["message"]:
            return func.HttpResponse("Unsupported update", status_code=400)

        message = body["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if text == "/start":
            save_session(chat_id, "awaiting_selection")
            inline_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Avvia nuova sessione", callback_data="new_session")],
                [InlineKeyboardButton("Ho un nome di sessione", callback_data="enter_session_code")],
            ])
            await bot.send_message(
                chat_id,
                "Vuoi avviare una nuova sessione o hai già un nome di sessione?",
                reply_markup=inline_keyboard,
            )
            return func.HttpResponse("OK", status_code=200)

        existing = get_session_entity(chat_id)
        if existing and existing.get("sessionName") and text.lower() == existing["sessionName"].lower():
            await bot.send_message(chat_id, f"Sessione recuperata: {existing['sessionName']}")
            return func.HttpResponse("OK", status_code=200)

        if text and text.strip():
            session_name = text.strip().lower()
            match = get_session_by_name(session_name)
            if match and str(match.get("chatId")) != str(chat_id):
                await bot.send_message(
                    chat_id,
                    f"Sessione trovata: {match.get('sessionName')}\n\nTi sto riportando alla chat associata.",
                )
                return func.HttpResponse("OK", status_code=200)

            if not existing:
                await bot.send_message(
                    chat_id,
                    "Non ho trovato una sessione associata a questo nome. Inizia con /start per creare una nuova sessione.",
                )

    return func.HttpResponse("OK", status_code=200)
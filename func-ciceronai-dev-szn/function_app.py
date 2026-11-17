import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import azure.functions as func
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient
from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from fantasy_names import ADJECTIVES, NOUNS

app = func.FunctionApp()

NEW_CHARACTER_PROMPT = "Descrivi il tuo personaggio nel formato nome | descrizione."
SESSION_CODE_PROMPT = "Scrivi il nome della sessione da recuperare."
JOIN_CHARACTER_PROMPT_PREFIX = "Descrivi il personaggio per la sessione "
JOIN_CHARACTER_PROMPT_SUFFIX = " nel formato nome | descrizione."
START_SESSION_CALLBACK_PREFIX = "start_session:"
SESSION_OPENING_MESSAGE = "C'era una volta blablabla"


def generate_session_name() -> str:
    return f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"


def parse_character(text: str) -> dict | None:
    parts = text.split("|", 1)
    if len(parts) != 2:
        return None

    name, description = (part.strip() for part in parts)
    if not name or not description:
        return None

    return {"name": name, "desc": description, "stats": {}}


def is_valid_session_code(session_code: str) -> bool:
    return bool(session_code) and (
        session_code[0].isalnum()
        and session_code[-1].isalnum()
        and all(
            character.isascii() and (character.isalnum() or character == "-")
            for character in session_code
        )
    )


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


def get_session_characters(session: dict | None) -> list[dict]:
    if not session:
        return []

    try:
        characters = json.loads(session.get("characters", "[]"))
    except (TypeError, json.JSONDecodeError):
        logging.warning("Session %s has invalid characters data.", session.get("sessionCode"))
        return []

    return characters if isinstance(characters, list) else []


def save_session(
    chat_id: int,
    status: str,
    session_name: str | None = None,
    character: dict | None = None,
) -> str:
    table_client = get_table_client()
    if not session_name:
        session_name = generate_session_name()
        while get_session_by_name(session_name) is not None:
            session_name = generate_session_name()

    existing_session = get_session_by_name(session_name)
    characters = get_session_characters(existing_session)
    if character:
        characters.append(character)

    now = datetime.now(timezone.utc).isoformat()
    table_client.upsert_entity({
        "PartitionKey": session_name,
        "RowKey": "session",
        "chatId": existing_session.get("chatId", str(chat_id)) if existing_session else str(chat_id),
        "sessionCode": session_name,
        "displayName": session_name,
        "status": status,
        "characters": json.dumps(characters, ensure_ascii=False),
        "createdAt": existing_session.get("createdAt", now) if existing_session else now,
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
            if callback_data and callback_data.startswith(START_SESSION_CALLBACK_PREFIX):
                session_code = callback_data[len(START_SESSION_CALLBACK_PREFIX):]
                session = get_session_by_name(session_code)
                user_id = callback_query.get("from", {}).get("id")
                characters = get_session_characters(session)

                if not session:
                    await bot.send_message(chat_id, "Non ho trovato questa sessione.")
                elif not any(str(character.get("telegramUserId")) == str(user_id) for character in characters):
                    await bot.send_message(chat_id, "Solo i giocatori registrati possono avviare la sessione.")
                elif session.get("status") == "started":
                    await bot.send_message(chat_id, "La sessione è già iniziata.")
                else:
                    save_session(
                        int(session.get("chatId", chat_id)),
                        "started",
                        session_name=session_code,
                    )
                    recipient_chat_ids = {
                        int(character["telegramChatId"])
                        for character in characters
                        if character.get("telegramChatId") is not None
                    }
                    for recipient_chat_id in recipient_chat_ids:
                        try:
                            await bot.send_message(recipient_chat_id, SESSION_OPENING_MESSAGE)
                        except TelegramError:
                            logging.exception(
                                "Could not send session opening to chat %s.",
                                recipient_chat_id,
                            )

                return func.HttpResponse("OK", status_code=200)

            if callback_data == "new_session":
                await bot.send_message(
                    chat_id,
                    NEW_CHARACTER_PROMPT,
                    reply_markup=ForceReply(selective=True),
                )
            elif callback_data == "enter_session_code":
                await bot.send_message(
                    chat_id,
                    SESSION_CODE_PROMPT,
                    reply_markup=ForceReply(selective=True),
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
            reply_to_message = message.get("reply_to_message", {})
            prompt_text = reply_to_message.get("text", "")

            if prompt_text == NEW_CHARACTER_PROMPT:
                character = parse_character(text)
                if character is None:
                    await bot.send_message(
                        chat_id,
                        NEW_CHARACTER_PROMPT,
                        reply_markup=ForceReply(selective=True),
                    )
                    return func.HttpResponse("OK", status_code=200)

                character["telegramUserId"] = message.get("from", {}).get("id")
                character["telegramChatId"] = chat_id
                session_name = save_session(chat_id, "active", character=character)
                start_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Avvia Sessione",
                        callback_data=f"{START_SESSION_CALLBACK_PREFIX}{session_name}",
                    )
                ]])
                await bot.send_message(
                    chat_id,
                    f"Benvenuto! La tua sessione è pronta.\n\nNome della chat: {session_name}\nCodice sessione: {session_name}\n\nIl tuo personaggio è registrato. Puoi avviare la sessione ora oppure attendere altri giocatori.",
                    reply_markup=start_keyboard,
                )
                return func.HttpResponse("OK", status_code=200)

            if prompt_text == SESSION_CODE_PROMPT:
                session = get_session_by_name(session_code) if is_valid_session_code(session_code) else None
                if not session:
                    await bot.send_message(
                        chat_id,
                        "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                    )
                    return func.HttpResponse("OK", status_code=200)
                if session.get("status") == "started":
                    await bot.send_message(chat_id, "Questa sessione è già iniziata e non accetta altri giocatori.")
                    return func.HttpResponse("OK", status_code=200)

                await bot.send_message(
                    chat_id,
                    f"{JOIN_CHARACTER_PROMPT_PREFIX}{session_code}{JOIN_CHARACTER_PROMPT_SUFFIX}",
                    reply_markup=ForceReply(selective=True),
                )
                return func.HttpResponse("OK", status_code=200)

            if (
                prompt_text.startswith(JOIN_CHARACTER_PROMPT_PREFIX)
                and prompt_text.endswith(JOIN_CHARACTER_PROMPT_SUFFIX)
            ):
                session_code = prompt_text[
                    len(JOIN_CHARACTER_PROMPT_PREFIX):-len(JOIN_CHARACTER_PROMPT_SUFFIX)
                ]
                session = get_session_by_name(session_code) if is_valid_session_code(session_code) else None
                character = parse_character(text)
                if not session:
                    await bot.send_message(
                        chat_id,
                        "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                    )
                elif session.get("status") == "started":
                    await bot.send_message(chat_id, "Questa sessione è già iniziata e non accetta altri giocatori.")
                elif character is None:
                    await bot.send_message(
                        chat_id,
                        prompt_text,
                        reply_markup=ForceReply(selective=True),
                    )
                else:
                    character["telegramUserId"] = message.get("from", {}).get("id")
                    character["telegramChatId"] = chat_id
                    save_session(chat_id, "active", session_name=session_code, character=character)
                    start_keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "Avvia Sessione",
                            callback_data=f"{START_SESSION_CALLBACK_PREFIX}{session_code}",
                        )
                    ]])
                    await bot.send_message(
                        chat_id,
                        f"Sessione recuperata: {session['displayName']}. Il tuo personaggio è registrato. Puoi avviare la sessione ora oppure attendere altri giocatori.",
                        reply_markup=start_keyboard,
                    )
                return func.HttpResponse("OK", status_code=200)

            session = get_session_by_name(session_code) if is_valid_session_code(session_code) else None
            if session:
                await bot.send_message(
                    chat_id,
                    "Per unirti, inizia con /start e scegli Ho un codice di sessione.",
                )
            else:
                await bot.send_message(
                    chat_id,
                    "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                )

    return func.HttpResponse("OK", status_code=200)
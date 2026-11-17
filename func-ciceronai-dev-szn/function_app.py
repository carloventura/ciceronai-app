import hashlib
import hmac
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import azure.functions as func
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode
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
TELEGRAM_UPDATES_QUEUE = os.getenv("TELEGRAM_UPDATES_QUEUE", "telegram-updates")
PROCESSED_UPDATES_PARTITION = "telegram"


def generate_session_name(seed: int | None = None, attempt: int = 0) -> str:
    chooser = random.Random(f"{seed}:{attempt}") if seed is not None else random
    return f"{chooser.choice(ADJECTIVES)}-{chooser.choice(NOUNS)}"


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
def get_table_service_client() -> TableServiceClient:
    connection_string = os.getenv("AzureWebJobsStorage")
    if connection_string:
        return TableServiceClient.from_connection_string(connection_string)

    account_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
    if not account_name or not account_key:
        raise RuntimeError("Missing Azure Table Storage configuration.")
    return TableServiceClient(
        endpoint=f"https://{account_name}.table.core.windows.net",
        credential=AzureNamedKeyCredential(account_name, account_key),
    )


@lru_cache(maxsize=2)
def get_table_client(table_name: str | None = None) -> TableClient:
    table_name = table_name or os.getenv("CHAT_SESSIONS_TABLE_NAME", "ChatSessions")
    service = get_table_service_client()
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


def get_processed_update(update_id: int) -> dict | None:
    try:
        return get_table_client("ProcessedUpdates").get_entity(
            partition_key=PROCESSED_UPDATES_PARTITION,
            row_key=str(update_id),
        )
    except ResourceNotFoundError:
        return None


def begin_update_processing(update_id: int) -> int | None:
    table_client = get_table_client("ProcessedUpdates")
    entity = get_processed_update(update_id)
    if entity and entity.get("status") == "completed":
        return None

    attempt = int(entity.get("attempts", 0)) + 1 if entity else 1
    now = datetime.now(timezone.utc).isoformat()
    table_client.upsert_entity(
        {
            "PartitionKey": PROCESSED_UPDATES_PARTITION,
            "RowKey": str(update_id),
            "updateId": update_id,
            "status": "processing",
            "attempts": attempt,
            "lastUpdated": now,
        },
        mode=UpdateMode.MERGE,
    )
    return attempt


def set_update_status(update_id: int, status: str) -> None:
    get_table_client("ProcessedUpdates").upsert_entity(
        {
            "PartitionKey": PROCESSED_UPDATES_PARTITION,
            "RowKey": str(update_id),
            "updateId": update_id,
            "status": status,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        },
        mode=UpdateMode.MERGE,
    )


def claim_telegram_message(update_id: int, message_key: str) -> bool:
    message_hash = hashlib.sha256(message_key.encode("utf-8")).hexdigest()
    try:
        get_table_client("ProcessedUpdates").create_entity(
            {
                "PartitionKey": PROCESSED_UPDATES_PARTITION,
                "RowKey": f"send-{update_id}-{message_hash}",
                "updateId": update_id,
                "messageKey": message_key,
                "status": "claimed",
                "lastUpdated": datetime.now(timezone.utc).isoformat(),
            }
        )
    except ResourceExistsError:
        return False
    return True


async def send_telegram_message_once(
    bot: Bot,
    update_id: int,
    message_key: str,
    chat_id: int,
    text: str,
    **kwargs,
):
    if not claim_telegram_message(update_id, message_key):
        logging.info("Skipping duplicate Telegram send for update %s (%s).", update_id, message_key)
        return None
    return await bot.send_message(chat_id, text, **kwargs)


def save_session(
    chat_id: int,
    status: str,
    session_name: str | None = None,
    character: dict | None = None,
    update_id: int | None = None,
) -> str:
    table_client = get_table_client()
    if not session_name:
        attempt = 0
        while True:
            candidate = generate_session_name(update_id, attempt)
            candidate_session = get_session_by_name(candidate)
            candidate_characters = get_session_characters(candidate_session)
            if candidate_session is None or any(
                str(existing_character.get("_telegramUpdateId")) == str(update_id)
                for existing_character in candidate_characters
            ):
                session_name = candidate
                break
            attempt += 1

    existing_session = get_session_by_name(session_name)
    characters = get_session_characters(existing_session)
    if character:
        character["_telegramUpdateId"] = str(update_id) if update_id is not None else None
        if update_id is None or not any(
            str(existing_character.get("_telegramUpdateId")) == str(update_id)
            for existing_character in characters
        ):
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
    for table_client in (get_table_client(), get_table_client("ProcessedUpdates")):
        for entity in table_client.list_entities():
            last_updated = entity.get("lastUpdated")
            if not last_updated:
                continue

            try:
                last_updated_dt = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
            except ValueError:
                logging.warning("Skipping entity with invalid lastUpdated value.")
                continue

            if last_updated_dt.tzinfo is None:
                last_updated_dt = last_updated_dt.replace(tzinfo=timezone.utc)

            if last_updated_dt < cutoff:
                table_client.delete_entity(entity["PartitionKey"], entity["RowKey"])
                logging.info("Deleted expired record from %s.", table_client.table_name)


@app.queue_output(
    arg_name="queue_message",
    queue_name=TELEGRAM_UPDATES_QUEUE,
    connection="AzureWebJobsStorage",
)
@app.route(
    route="ChatMessage",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=[func.HttpMethod.POST],
)
def ChatMessage(
    req: func.HttpRequest,
    queue_message: func.Out[str],
) -> func.HttpResponse:
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    provided_secret = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected_secret or not hmac.compare_digest(provided_secret, expected_secret):
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    if (
        not isinstance(body, dict)
        or not isinstance(body.get("update_id"), int)
        or isinstance(body.get("update_id"), bool)
    ):
        return func.HttpResponse("Invalid Telegram update", status_code=400)

    queue_message.set(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    return func.HttpResponse("OK", status_code=200)


@app.queue_trigger(
    arg_name="queue_message",
    queue_name=TELEGRAM_UPDATES_QUEUE,
    connection="AzureWebJobsStorage",
)
async def ProcessTelegramUpdate(queue_message: func.QueueMessage) -> None:
    try:
        update = json.loads(queue_message.get_body().decode("utf-8"))
        update_id = update["update_id"]
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            raise ValueError("Invalid update_id in queue message.")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logging.error("Discarding malformed Telegram queue message (%s).", type(exc).__name__)
        raise

    attempt = begin_update_processing(update_id)
    if attempt is None:
        logging.info("Skipping already processed Telegram update %s.", update_id)
        return

    try:
        await process_telegram_update(update, update_id, attempt)
    except Exception:
        set_update_status(update_id, "failed")
        logging.exception("Failed processing Telegram update %s.", update_id)
        raise
    set_update_status(update_id, "completed")


async def process_telegram_update(update: dict, update_id: int, attempt: int) -> None:
    async with Bot(os.environ["TELEGRAM_TOKEN"]) as bot:
        if "callback_query" in update:
            callback_query = update["callback_query"]
            try:
                await bot.answer_callback_query(callback_query["id"])
            except TelegramError:
                logging.info("Callback query for update %s was already acknowledged.", update_id)

            chat_id = callback_query["message"]["chat"]["id"]
            callback_data = callback_query.get("data")
            if callback_data and callback_data.startswith(START_SESSION_CALLBACK_PREFIX):
                session_code = callback_data[len(START_SESSION_CALLBACK_PREFIX):]
                session = get_session_by_name(session_code)
                user_id = callback_query.get("from", {}).get("id")
                characters = get_session_characters(session)

                if not session:
                    await send_telegram_message_once(bot, update_id, "session_missing", chat_id, "Non ho trovato questa sessione.")
                elif not any(str(character.get("telegramUserId")) == str(user_id) for character in characters):
                    await send_telegram_message_once(bot, update_id, "start_not_registered", chat_id, "Solo i giocatori registrati possono avviare la sessione.")
                elif session.get("status") == "started" and attempt == 1:
                    await send_telegram_message_once(bot, update_id, "session_already_started", chat_id, "La sessione è già iniziata.")
                else:
                    if session.get("status") != "started":
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
                        await send_telegram_message_once(
                            bot,
                            update_id,
                            f"session_opening:{recipient_chat_id}",
                            recipient_chat_id,
                            SESSION_OPENING_MESSAGE,
                        )
                return

            if callback_data == "new_session":
                await send_telegram_message_once(
                    bot, update_id, "new_character_prompt", chat_id, NEW_CHARACTER_PROMPT,
                    reply_markup=ForceReply(selective=True),
                )
            elif callback_data == "enter_session_code":
                await send_telegram_message_once(
                    bot, update_id, "session_code_prompt", chat_id, SESSION_CODE_PROMPT,
                    reply_markup=ForceReply(selective=True),
                )
            return

        if "message" not in update or "chat" not in update["message"]:
            return

        message = update["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()

        if text == "/start":
            inline_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Avvia nuova sessione", callback_data="new_session")],
                [InlineKeyboardButton("Ho un codice di sessione", callback_data="enter_session_code")],
            ])
            await send_telegram_message_once(
                bot,
                update_id,
                "start_menu",
                chat_id,
                "Vuoi avviare una nuova sessione o hai già un codice di sessione?",
                reply_markup=inline_keyboard,
            )
            return

        if not text:
            return

        session_code = text.lower()
        reply_to_message = message.get("reply_to_message", {})
        prompt_text = reply_to_message.get("text", "")

        if prompt_text == NEW_CHARACTER_PROMPT:
            character = parse_character(text)
            if character is None:
                await send_telegram_message_once(
                    bot, update_id, "new_character_prompt", chat_id, NEW_CHARACTER_PROMPT,
                    reply_markup=ForceReply(selective=True),
                )
                return

            character["telegramUserId"] = message.get("from", {}).get("id")
            character["telegramChatId"] = chat_id
            session_name = save_session(chat_id, "active", character=character, update_id=update_id)
            start_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Avvia Sessione",
                    callback_data=f"{START_SESSION_CALLBACK_PREFIX}{session_name}",
                )
            ]])
            await send_telegram_message_once(
                bot,
                update_id,
                "new_session_created",
                chat_id,
                f"Benvenuto! La tua sessione è pronta.\n\nNome della chat: {session_name}\nCodice sessione: {session_name}\n\nIl tuo personaggio è registrato. Puoi avviare la sessione ora oppure attendere altri giocatori.",
                reply_markup=start_keyboard,
            )
            return

        if prompt_text == SESSION_CODE_PROMPT:
            session = get_session_by_name(session_code) if is_valid_session_code(session_code) else None
            if not session:
                await send_telegram_message_once(
                    bot, update_id, "session_not_found", chat_id,
                    "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                )
                return
            if session.get("status") == "started":
                await send_telegram_message_once(
                    bot, update_id, "session_started_join_rejected", chat_id,
                    "Questa sessione è già iniziata e non accetta altri giocatori.",
                )
                return

            await send_telegram_message_once(
                bot,
                update_id,
                "join_character_prompt",
                chat_id,
                f"{JOIN_CHARACTER_PROMPT_PREFIX}{session_code}{JOIN_CHARACTER_PROMPT_SUFFIX}",
                reply_markup=ForceReply(selective=True),
            )
            return

        if prompt_text.startswith(JOIN_CHARACTER_PROMPT_PREFIX) and prompt_text.endswith(JOIN_CHARACTER_PROMPT_SUFFIX):
            session_code = prompt_text[
                len(JOIN_CHARACTER_PROMPT_PREFIX):-len(JOIN_CHARACTER_PROMPT_SUFFIX)
            ]
            session = get_session_by_name(session_code) if is_valid_session_code(session_code) else None
            character = parse_character(text)
            if not session:
                await send_telegram_message_once(
                    bot, update_id, "session_not_found", chat_id,
                    "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                )
            elif session.get("status") == "started":
                await send_telegram_message_once(
                    bot, update_id, "session_started_join_rejected", chat_id,
                    "Questa sessione è già iniziata e non accetta altri giocatori.",
                )
            elif character is None:
                await send_telegram_message_once(
                    bot, update_id, "join_character_prompt", chat_id, prompt_text,
                    reply_markup=ForceReply(selective=True),
                )
            else:
                character["telegramUserId"] = message.get("from", {}).get("id")
                character["telegramChatId"] = chat_id
                save_session(
                    chat_id,
                    "active",
                    session_name=session_code,
                    character=character,
                    update_id=update_id,
                )
                start_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Avvia Sessione",
                        callback_data=f"{START_SESSION_CALLBACK_PREFIX}{session_code}",
                    )
                ]])
                await send_telegram_message_once(
                    bot,
                    update_id,
                    "joined_session",
                    chat_id,
                    f"Sessione recuperata: {session['displayName']}. Il tuo personaggio è registrato. Puoi avviare la sessione ora oppure attendere altri giocatori.",
                    reply_markup=start_keyboard,
                )
            return

        session = get_session_by_name(session_code) if is_valid_session_code(session_code) else None
        if session:
            await send_telegram_message_once(
                bot, update_id, "join_via_start", chat_id,
                "Per unirti, inizia con /start e scegli Ho un codice di sessione.",
            )
        else:
            await send_telegram_message_once(
                bot, update_id, "session_not_found", chat_id,
                "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
            )
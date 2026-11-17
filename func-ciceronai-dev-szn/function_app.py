import hashlib
import hmac
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable

import azure.functions as func
from azure.core import MatchConditions
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode
from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from fantasy_names import ADJECTIVES, NOUNS
from master import generate_stats, narrate, summarize

app = func.FunctionApp()

NEW_CHARACTER_PROMPT = "Descrivi il tuo personaggio nel formato nome | descrizione."
SESSION_CODE_PROMPT = "Scrivi il nome della sessione da recuperare."
JOIN_CHARACTER_PROMPT_PREFIX = "Descrivi il personaggio per la sessione "
JOIN_CHARACTER_PROMPT_SUFFIX = " nel formato nome | descrizione."
START_SESSION_CALLBACK_PREFIX = "start_session:"
TELEGRAM_UPDATES_QUEUE = os.getenv("TELEGRAM_UPDATES_QUEUE", "telegram-updates")
PROCESSED_UPDATES_PARTITION = "telegram"
MAX_RECENT_MESSAGES = 6
MAX_TABLE_PROPERTY_BYTES = 60 * 1024
MAX_PLAYER_MESSAGE_CHARS = 4000
SESSION_UPDATE_RETRIES = 5
PLAYER_ACTION_INTERVAL_SECONDS = 5


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


def get_session_json(session: dict, property_name: str, default):
    value = session.get(property_name)
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        logging.warning("Session %s has invalid %s data.", session.get("sessionCode"), property_name)
        return default
    return parsed


def get_session_summary(session: dict) -> str:
    raw_summary = session.get("summary", "")
    try:
        summary = json.loads(raw_summary)
    except (TypeError, json.JSONDecodeError):
        return str(raw_summary)
    return summary if isinstance(summary, str) else str(raw_summary)


def serialize_table_property(value, property_name: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_TABLE_PROPERTY_BYTES:
        raise ValueError(f"La proprietà Azure Table {property_name} supera il limite di sicurezza.")
    return encoded


def is_etag_conflict(error: HttpResponseError) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    return status_code == 412


def try_update_session_entity(session: dict, changes: dict) -> bool:
    etag = getattr(session, "metadata", {}).get("etag")
    if not etag:
        raise RuntimeError("La sessione non contiene un ETag utilizzabile.")

    entity = {
        "PartitionKey": session["PartitionKey"],
        "RowKey": session["RowKey"],
        **changes,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    }
    try:
        get_table_client().update_entity(
            entity=entity,
            mode=UpdateMode.MERGE,
            etag=etag,
            match_condition=MatchConditions.IfNotModified,
        )
    except HttpResponseError as exc:
        if is_etag_conflict(exc):
            return False
        raise
    return True


def update_session_with_retry(
    session_name: str,
    mutate: Callable[[dict], dict | None],
    retries: int = SESSION_UPDATE_RETRIES,
) -> dict:
    for _ in range(retries):
        session = get_session_by_name(session_name)
        if session is None:
            raise LookupError(f"Sessione {session_name} non trovata.")
        changes = mutate(session)
        if changes is None:
            return dict(session)
        if try_update_session_entity(session, changes):
            updated = dict(session)
            updated.update(changes)
            updated["lastUpdated"] = datetime.now(timezone.utc).isoformat()
            return updated
    raise RuntimeError(f"Conflitto ETag persistente aggiornando la sessione {session_name}.")


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


def set_update_data(update_id: int, **properties) -> None:
    get_table_client("ProcessedUpdates").upsert_entity(
        {
            "PartitionKey": PROCESSED_UPDATES_PARTITION,
            "RowKey": str(update_id),
            "updateId": update_id,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            **properties,
        },
        mode=UpdateMode.MERGE,
    )


def register_player_session(user_id: int | None, chat_id: int, session_name: str) -> None:
    if user_id is None:
        return
    get_table_client("ProcessedUpdates").upsert_entity(
        {
            "PartitionKey": f"player-{user_id}",
            "RowKey": session_name,
            "telegramUserId": user_id,
            "telegramChatId": str(chat_id),
            "sessionCode": session_name,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        },
        mode=UpdateMode.MERGE,
    )


def get_player_session(user_id: int | None, chat_id: int) -> dict | None:
    if user_id is None:
        return None
    table_client = get_table_client("ProcessedUpdates")
    mappings = table_client.list_entities(filter=f"PartitionKey eq 'player-{user_id}'")
    matching = [
        mapping for mapping in mappings
        if str(mapping.get("telegramChatId")) == str(chat_id)
    ]
    matching.sort(key=lambda mapping: str(mapping.get("lastUpdated", "")), reverse=True)
    for mapping in matching:
        session = get_session_by_name(mapping["sessionCode"])
        if session and any(
            str(character.get("telegramUserId")) == str(user_id)
            for character in get_session_characters(session)
        ):
            return session

    for session in get_table_client().list_entities():
        if session.get("status") not in ("active", "started", "finished"):
            continue
        if any(
            str(character.get("telegramUserId")) == str(user_id)
            and str(character.get("telegramChatId")) == str(chat_id)
            for character in get_session_characters(session)
        ):
            return session
    return None


def allow_player_action(user_id: int, update_id: int) -> bool:
    table_client = get_table_client("ProcessedUpdates")
    partition_key = "rate-limit"
    row_key = str(user_id)

    for _ in range(SESSION_UPDATE_RETRIES):
        now = datetime.now(timezone.utc)
        try:
            entity = table_client.get_entity(partition_key=partition_key, row_key=row_key)
        except ResourceNotFoundError:
            try:
                table_client.create_entity({
                    "PartitionKey": partition_key,
                    "RowKey": row_key,
                    "lastActionAt": now.isoformat(),
                    "lastUpdateId": update_id,
                    "lastUpdated": now.isoformat(),
                })
                return True
            except ResourceExistsError:
                continue

        if str(entity.get("lastUpdateId")) == str(update_id):
            return True
        try:
            last_action = datetime.fromisoformat(str(entity["lastActionAt"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            last_action = now - timedelta(seconds=PLAYER_ACTION_INTERVAL_SECONDS)
        if last_action.tzinfo is None:
            last_action = last_action.replace(tzinfo=timezone.utc)
        if (now - last_action).total_seconds() < PLAYER_ACTION_INTERVAL_SECONDS:
            return False

        etag = getattr(entity, "metadata", {}).get("etag")
        try:
            table_client.update_entity(
                entity={
                    "PartitionKey": partition_key,
                    "RowKey": row_key,
                    "lastActionAt": now.isoformat(),
                    "lastUpdateId": update_id,
                    "lastUpdated": now.isoformat(),
                },
                mode=UpdateMode.MERGE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except HttpResponseError as exc:
            if not is_etag_conflict(exc):
                raise
    return False


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
    if character:
        character = dict(character)
        character["_telegramUpdateId"] = str(update_id) if update_id is not None else None

    candidates = [session_name] if session_name else [
        generate_session_name(update_id, attempt) for attempt in range(1000)
    ]
    for candidate in candidates:
        existing_session = get_session_by_name(candidate)
        if existing_session is None:
            now = datetime.now(timezone.utc).isoformat()
            characters = [character] if character else []
            new_entity = {
                "PartitionKey": candidate,
                "RowKey": "session",
                "chatId": str(chat_id),
                "sessionCode": candidate,
                "displayName": candidate,
                "status": status,
                "characters": serialize_table_property(characters, "characters"),
                "summary": serialize_table_property("", "summary"),
                "recentMessages": "[]",
                "turns": 0,
                "createdAt": now,
                "lastUpdated": now,
            }
            try:
                table_client.create_entity(new_entity)
            except ResourceExistsError:
                continue
            if character:
                register_player_session(character.get("telegramUserId"), chat_id, candidate)
            return candidate

        existing_characters = get_session_characters(existing_session)
        is_same_update = update_id is not None and any(
            str(item.get("_telegramUpdateId")) == str(update_id)
            for item in existing_characters
        )
        if session_name is None and not is_same_update:
            continue

        def append_character(current: dict) -> dict:
            current_characters = get_session_characters(current)
            already_saved = update_id is not None and any(
                str(item.get("_telegramUpdateId")) == str(update_id)
                for item in current_characters
            )
            if current.get("status") in ("started", "finished") and not already_saved:
                return None
            changes = {}
            if character and not already_saved:
                current_characters.append(character)
            if character:
                changes["characters"] = serialize_table_property(current_characters, "characters")
            return changes

        updated_session = update_session_with_retry(candidate, append_character)
        if updated_session.get("status") in ("started", "finished") and character and not is_same_update:
            raise RuntimeError("La sessione non accetta altri personaggi.")
        if character:
            register_player_session(character.get("telegramUserId"), chat_id, candidate)
        return candidate

    raise RuntimeError("Impossibile generare un codice sessione univoco dopo 1000 tentativi.")


def get_max_turns_per_session() -> int:
    try:
        return max(1, int(os.getenv("MAX_TURNS_PER_SESSION", "50")))
    except ValueError:
        logging.warning("Invalid MAX_TURNS_PER_SESSION; using 50 turns.")
        return 50


def claim_session_start(session_name: str, update_id: int) -> dict | None:
    for _ in range(SESSION_UPDATE_RETRIES):
        session = get_session_by_name(session_name)
        if session is None:
            return None
        if session.get("status") == "started":
            return session if str(session.get("startedByUpdateId")) == str(update_id) else None
        if session.get("status") != "active":
            return None
        if try_update_session_entity(
            session,
            {"status": "started", "startedByUpdateId": str(update_id)},
        ):
            updated = dict(session)
            updated.update({"status": "started", "startedByUpdateId": str(update_id)})
            return updated
    raise RuntimeError(f"Conflitto ETag persistente avviando la sessione {session_name}.")


def get_cached_character_stats(update_id: int) -> tuple[dict, bool] | None:
    processed_update = get_processed_update(update_id)
    if not processed_update or "characterStats" not in processed_update:
        return None
    try:
        stats = json.loads(processed_update["characterStats"])
    except (TypeError, json.JSONDecodeError):
        return None
    return (stats if isinstance(stats, dict) else {}, bool(processed_update.get("statsGenerationFailed")))


def generate_character_stats(update_id: int, name: str, description: str) -> tuple[dict, bool]:
    cached = get_cached_character_stats(update_id)
    if cached is not None:
        return cached

    try:
        stats = generate_stats(name, description)
        failed = False
    except Exception as exc:
        logging.error("Character stats generation failed (%s).", type(exc).__name__)
        stats = {}
        failed = True

    set_update_data(
        update_id,
        characterStats=serialize_table_property(stats, "characterStats"),
        statsGenerationFailed=failed,
    )
    return stats, failed


def format_character_stats(stats: dict) -> str:
    if not stats:
        return "Statistiche non disponibili; potrai rigenerarle in seguito."

    lines = [f"{name.replace('_', ' ').capitalize()}: {value}" for name, value in stats.items()]
    return "Statistiche generate:\n" + "\n".join(lines)


def get_public_characters(session: dict) -> list[dict]:
    return [
        {key: value for key, value in character.items() if not key.startswith("_")}
        for character in get_session_characters(session)
    ]


def get_session_recent_messages(session: dict) -> list[dict]:
    messages = get_session_json(session, "recentMessages", [])
    return messages[-MAX_RECENT_MESSAGES:] if isinstance(messages, list) else []


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


async def send_session_opening(bot: Bot, session_name: str, update_id: int, click_chat_id: int) -> None:
    session = claim_session_start(session_name, update_id)
    if session is None:
        current = get_session_by_name(session_name)
        if current is None:
            await send_telegram_message_once(bot, update_id, "session_missing", click_chat_id, "Non ho trovato questa sessione.")
        elif current.get("status") == "finished":
            await send_telegram_message_once(bot, update_id, "session_finished", click_chat_id, "Questa sessione è già conclusa.")
        else:
            await send_telegram_message_once(bot, update_id, "session_already_started", click_chat_id, "La sessione è già iniziata.")
        return

    processed_update = get_processed_update(update_id) or {}
    opening = processed_update.get("openingText")
    if not opening:
        opening = narrate(
            get_session_summary(session),
            get_session_recent_messages(session),
            get_public_characters(session),
            action=None,
        )
        set_update_data(update_id, openingText=opening)

    def save_opening(current: dict) -> dict:
        if str(current.get("startedByUpdateId")) != str(update_id):
            return None
        recent = get_session_recent_messages(current)
        if any(str(item.get("updateId")) == str(update_id) for item in recent):
            return {}
        recent.append({"role": "master", "content": opening, "updateId": str(update_id)})
        summary = get_session_summary(current)
        if len(recent) > MAX_RECENT_MESSAGES:
            summary = summarize(summary, recent[:-MAX_RECENT_MESSAGES])
            recent = recent[-MAX_RECENT_MESSAGES:]
        return {
            "summary": serialize_table_property(summary, "summary"),
            "recentMessages": serialize_table_property(recent, "recentMessages"),
        }

    session = update_session_with_retry(session_name, save_opening)
    recipient_chat_ids = {
        int(character["telegramChatId"])
        for character in get_session_characters(session)
        if character.get("telegramChatId") is not None
    }
    for recipient_chat_id in recipient_chat_ids:
        await send_telegram_message_once(
            bot,
            update_id,
            f"session_opening:{recipient_chat_id}",
            recipient_chat_id,
            opening,
        )


async def register_character(
    bot: Bot,
    update_id: int,
    message: dict,
    text: str,
    session_name: str | None = None,
) -> None:
    chat_id = message["chat"]["id"]
    character = parse_character(text)
    if character is None:
        prompt = NEW_CHARACTER_PROMPT if session_name is None else f"{JOIN_CHARACTER_PROMPT_PREFIX}{session_name}{JOIN_CHARACTER_PROMPT_SUFFIX}"
        await send_telegram_message_once(
            bot, update_id, "character_prompt", chat_id, prompt,
            reply_markup=ForceReply(selective=True),
        )
        return

    user_id = message.get("from", {}).get("id")
    stats, _ = generate_character_stats(update_id, character["name"], character["desc"])
    character["stats"] = stats
    character["telegramUserId"] = user_id
    character["telegramChatId"] = chat_id
    saved_session_name = save_session(
        chat_id,
        "active",
        session_name=session_name,
        character=character,
        update_id=update_id,
    )
    set_update_data(update_id, sessionCode=saved_session_name)
    start_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Avvia Sessione",
            callback_data=f"{START_SESSION_CALLBACK_PREFIX}{saved_session_name}",
        )
    ]])
    introductory_text = (
        f"Benvenuto! La tua sessione è pronta.\n\nNome della chat: {saved_session_name}"
        f"\nCodice sessione: {saved_session_name}\n\nIl tuo personaggio è registrato. "
        "Puoi avviare la sessione ora oppure attendere altri giocatori."
        if session_name is None
        else f"Sessione recuperata: {saved_session_name}. Il tuo personaggio è registrato. "
        "Puoi avviare la sessione ora oppure attendere altri giocatori."
    )
    await send_telegram_message_once(
        bot,
        update_id,
        "character_registered",
        chat_id,
        f"{introductory_text}\n\n{format_character_stats(stats)}",
        reply_markup=start_keyboard,
    )


async def handle_player_action(
    bot: Bot,
    session_name: str,
    update_id: int,
    user_id: int,
    chat_id: int,
    player_name: str,
    action: str,
) -> None:
    processed_update = get_processed_update(update_id) or {}
    if "actionResponse" in processed_update:
        response_text = processed_update["actionResponse"]
        is_final_turn = bool(processed_update.get("actionFinal"))
        response_prefix = "Epilogo:\n\n" if is_final_turn else ""
        await send_telegram_message_once(
            bot,
            update_id,
            "master_turn_response",
            chat_id,
            f"{response_prefix}{response_text}",
        )
        return

    if not allow_player_action(user_id, update_id):
        await send_telegram_message_once(
            bot, update_id, "player_rate_limited", chat_id,
            "Aspetta qualche secondo prima di inviare un'altra azione.",
        )
        return

    bounded_action = action[:MAX_PLAYER_MESSAGE_CHARS]
    for _ in range(SESSION_UPDATE_RETRIES):
        session = get_session_by_name(session_name)
        if session is None:
            await send_telegram_message_once(bot, update_id, "session_missing", chat_id, "Non ho trovato questa sessione.")
            return
        recent = get_session_recent_messages(session)
        prior_response = next(
            (
                item for item in recent
                if item.get("role") == "assistant"
                and str(item.get("updateId")) == str(update_id)
            ),
            None,
        )
        if prior_response:
            response_text = prior_response.get("content", "")
            is_final_turn = bool(prior_response.get("isFinal"))
            break
        if session.get("status") == "finished":
            await send_telegram_message_once(bot, update_id, "session_finished", chat_id, "La sessione è conclusa.")
            return
        if session.get("status") != "started":
            return

        turns = int(session.get("turns", 0))
        max_turns = get_max_turns_per_session()
        if turns >= max_turns:
            try_update_session_entity(session, {"status": "finished"})
            await send_telegram_message_once(bot, update_id, "session_finished", chat_id, "La sessione è conclusa.")
            return

        summary = get_session_summary(session)
        is_final_turn = turns + 1 >= max_turns
        recent.append({
            "role": "user",
            "player": player_name,
            "content": bounded_action,
            "updateId": str(update_id),
        })
        if len(recent) > MAX_RECENT_MESSAGES:
            summary = summarize(summary, recent[:-MAX_RECENT_MESSAGES])
            recent = recent[-MAX_RECENT_MESSAGES:]

        narrative_action = bounded_action
        if is_final_turn:
            narrative_action += "\n\nQuesto è l'ultimo turno previsto: concludi la storia con un epilogo fantasy soddisfacente."
        response_text = narrate(
            summary,
            recent,
            get_public_characters(session),
            action=narrative_action,
        )
        recent.append({
            "role": "assistant",
            "content": response_text,
            "updateId": str(update_id),
            "isFinal": is_final_turn,
        })
        if len(recent) > MAX_RECENT_MESSAGES:
            summary = summarize(summary, recent[:-MAX_RECENT_MESSAGES])
            recent = recent[-MAX_RECENT_MESSAGES:]
        changes = {
            "summary": serialize_table_property(summary, "summary"),
            "recentMessages": serialize_table_property(recent, "recentMessages"),
            "turns": turns + 1,
        }
        if is_final_turn:
            changes["status"] = "finished"
        if try_update_session_entity(session, changes):
            set_update_data(
                update_id,
                actionResponse=response_text,
                actionFinal=is_final_turn,
                actionSession=session_name,
            )
            break
    else:
        raise RuntimeError(f"Conflitto ETag persistente elaborando un turno di {session_name}.")

    response_prefix = "Epilogo:\n\n" if is_final_turn else ""
    await send_telegram_message_once(
        bot,
        update_id,
        "master_turn_response",
        chat_id,
        f"{response_prefix}{response_text}",
    )


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
            user_id = callback_query.get("from", {}).get("id")
            player_session = get_player_session(user_id, chat_id)
            if player_session:
                update_session_with_retry(player_session["sessionCode"], lambda _: {})
            if callback_data and callback_data.startswith(START_SESSION_CALLBACK_PREFIX):
                session_code = callback_data[len(START_SESSION_CALLBACK_PREFIX):]
                session = get_session_by_name(session_code)
                if session is None:
                    await send_telegram_message_once(bot, update_id, "session_missing", chat_id, "Non ho trovato questa sessione.")
                elif not any(
                    str(character.get("telegramUserId")) == str(user_id)
                    for character in get_session_characters(session)
                ):
                    await send_telegram_message_once(bot, update_id, "start_not_registered", chat_id, "Solo i giocatori registrati possono avviare la sessione.")
                else:
                    await send_session_opening(bot, session_code, update_id, chat_id)
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
        user_id = message.get("from", {}).get("id")
        interaction_session = get_player_session(user_id, chat_id)
        if interaction_session:
            update_session_with_retry(interaction_session["sessionCode"], lambda _: {})
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
            await register_character(bot, update_id, message, text)
            return

        if prompt_text == SESSION_CODE_PROMPT:
            session = get_session_by_name(session_code) if is_valid_session_code(session_code) else None
            if not session:
                await send_telegram_message_once(
                    bot, update_id, "session_not_found", chat_id,
                    "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                )
                return
            if session.get("status") != "active":
                await send_telegram_message_once(
                    bot, update_id, "session_not_joinable", chat_id,
                    "Questa sessione non accetta altri giocatori.",
                )
                return
            session = update_session_with_retry(session_code, lambda _: {})
            if session.get("status") != "active":
                await send_telegram_message_once(
                    bot, update_id, "session_not_joinable", chat_id,
                    "Questa sessione non accetta altri giocatori.",
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
            if not session:
                await send_telegram_message_once(
                    bot, update_id, "session_not_found", chat_id,
                    "Non ho trovato una sessione con questo nome. Inizia con /start per crearne una.",
                )
            elif session.get("status") != "active":
                await send_telegram_message_once(
                    bot, update_id, "session_not_joinable", chat_id,
                    "Questa sessione non accetta altri giocatori.",
                )
            else:
                await register_character(bot, update_id, message, text, session_name=session_code)
            return

        session = get_player_session(user_id, chat_id)
        if session and session.get("status") == "started":
            await handle_player_action(
                bot,
                session["sessionCode"],
                update_id,
                user_id,
                chat_id,
                message.get("from", {}).get("username") or message.get("from", {}).get("first_name") or str(user_id),
                text,
            )
            return
        if session and session.get("status") == "finished":
            await send_telegram_message_once(bot, update_id, "session_finished", chat_id, "La sessione è conclusa.")
            return
        if session and session.get("status") == "active":
            update_session_with_retry(session["sessionCode"], lambda _: {})

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
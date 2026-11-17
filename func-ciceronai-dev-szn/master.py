import json
import logging
import os
from functools import lru_cache
from typing import Any

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

_AZURE_OPENAI_SCOPE = "https://ai.azure.com/.default"
_DEFAULT_MAX_TOKENS = 400
_RECENT_MESSAGE_LIMIT = 12
_SUMMARY_WORD_LIMIT = 200
_NARRATION_WORD_LIMIT = 150


# class CharacterStats(BaseModel):
#     model_config = ConfigDict(extra="forbid")

#     forza: int = Field(ge=1, le=20, description="Forza, da 1 a 20.")
#     destrezza: int = Field(ge=1, le=20, description="Destrezza, da 1 a 20.")
#     costituzione: int = Field(ge=1, le=20, description="Costituzione, da 1 a 20.")
#     intelligenza: int = Field(ge=1, le=20, description="Intelligenza, da 1 a 20.")
#     saggezza: int = Field(ge=1, le=20, description="Saggezza, da 1 a 20.")
#     carisma: int = Field(ge=1, le=20, description="Carisma, da 1 a 20.")
#     hp: int = Field(ge=1, description="Punti ferita massimi.")
#     abilita: list[str] = Field(description="Lista breve, massimo cinque abilità.")

#     @field_validator("abilita")
#     @classmethod
#     def limit_abilities(cls, abilities: list[str]) -> list[str]:
#         if len(abilities) > 5:
#             raise ValueError("Sono consentite al massimo cinque abilità.")
#         if any(not ability.strip() for ability in abilities):
#             raise ValueError("Le abilità non possono essere vuote.")
#         return abilities
class CharacterStats(BaseModel):
     fisico: int = Field(ge=1, le=10, description="Fisico, da 1 a 10.")
     mentale: int = Field(ge=1, le=10, description="Mentale, da 1 a 10.")

@lru_cache(maxsize=1)
def _get_client() -> OpenAI:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Variabile AZURE_OPENAI_ENDPOINT mancante.")

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(credential, _AZURE_OPENAI_SCOPE)
    base_url = f"{endpoint.rstrip('/')}/openai/v1/"
    return OpenAI(
        base_url=base_url,
        api_key=token_provider,
    )


def _get_deployment(variable_name: str) -> str:
    deployment = os.getenv(variable_name)
    if not deployment:
        raise RuntimeError(f"Variabile {variable_name} mancante.")
    return deployment


def _get_max_tokens() -> int:
    value = os.getenv("AZURE_OPENAI_MAX_TOKENS")
    if value is None:
        return _DEFAULT_MAX_TOKENS

    try:
        max_tokens = int(value)
    except ValueError:
        logger.warning("AZURE_OPENAI_MAX_TOKENS non valido; uso il valore predefinito.")
        return _DEFAULT_MAX_TOKENS

    if max_tokens < 1:
        logger.warning("AZURE_OPENAI_MAX_TOKENS deve essere positivo; uso il valore predefinito.")
        return _DEFAULT_MAX_TOKENS
    return max_tokens


def _log_usage(operation: str, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        logger.info("Azure OpenAI %s: token usage non disponibile.", operation)
        return

    logger.info(
        "Azure OpenAI %s token usage: prompt=%s completion=%s total=%s",
        operation,
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
        getattr(usage, "total_tokens", None),
    )


def _create_completion(operation: str, **kwargs: Any) -> Any:
    try:
        response = _get_client().chat.completions.create(**kwargs)
    except Exception as exc:
        logger.error("Azure OpenAI %s non riuscito (%s).", operation, type(exc).__name__)
        raise RuntimeError(f"Chiamata Azure OpenAI per {operation} non riuscita.") from exc

    _log_usage(operation, response)
    return response


def _response_text(response: Any, operation: str) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Azure OpenAI non ha restituito testo per {operation}.") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"Azure OpenAI ha restituito una risposta vuota per {operation}.")
    return content.strip()


def _limit_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit])


def narrate(
    summary: str,
    recent_messages: list[dict],
    characters: list[dict],
    action: str | None,
) -> str:
    """Produce un breve passaggio narrativo usando solo contesto recente e riassunto."""
    system_prompt = (
        "Sei il Master di un gioco di ruolo fantasy. Narra in seconda o terza persona, "
        "con risposte concise di massimo circa 150 parole. Non decidere né descrivere "
        "al posto dei giocatori le azioni, le parole o i pensieri dei loro personaggi. "
        "Rispetta il riassunto, i messaggi recenti e le schede fornite. Termina sempre "
        "con una situazione aperta che inviti i giocatori ad agire. Se non è indicata "
        "un'azione, crea l'incipit della storia a partire dai personaggi."
    )
    task = (
        "Scrivi l'incipit della storia, introducendo i personaggi e una situazione "
        "fantasy che permetta loro di scegliere come agire."
        if action is None
        else f"Prosegui la narrazione reagendo a questa azione dichiarata: {action}"
    )
    context = {
        "riassunto_storia": summary,
        "ultimi_messaggi": recent_messages[-_RECENT_MESSAGE_LIMIT:],
        "personaggi": characters,
        "compito": task,
    }

    response = _create_completion(
        "narrate",
        model=_get_deployment("AZURE_OPENAI_NARRATOR_DEPLOYMENT"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        max_tokens=_get_max_tokens(),
        temperature=0.7,
    )
    return _limit_words(_response_text(response, "narrate"), _NARRATION_WORD_LIMIT)


def generate_stats(name: str, desc: str) -> dict:
    """Genera statistiche validate; riprova una sola volta in caso di errore di schema."""
    deployment = _get_deployment("AZURE_OPENAI_STATS_DEPLOYMENT")
    instructions = (
        "Genera statistiche bilanciate per un personaggio fantasy basandoti sul nome "
        "e sulla descrizione. Restituisci solo i campi richiesti. Le due caratteristiche "
        "devono essere interi tra 1 e 10. Non aggiungere campi extra."
    )
    last_validation_error: Exception | None = None

    for attempt in range(2):
        attempt_instructions = instructions
        if attempt:
            attempt_instructions += (
                " Rispetta rigorosamente i tipi, gli intervalli e il numero massimo "
                "di abilità definiti dallo schema."
            )

        try:
            response = _get_client().chat.completions.parse(
                model=deployment,
                messages=[
                    {"role": "system", "content": attempt_instructions},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"nome": name, "descrizione": desc},
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_format=CharacterStats,
                max_tokens=_get_max_tokens(),
                temperature=0.1,
            )
            _log_usage("generate_stats", response)
            parsed = response.choices[0].message.parsed
            if parsed is None:
                content = response.choices[0].message.content
                if not content:
                    raise ValueError("Risposta strutturata vuota.")
                parsed = CharacterStats.model_validate_json(content)
            else:
                parsed = CharacterStats.model_validate(parsed.model_dump())
            return parsed.model_dump()
        except ValidationError as exc:
            last_validation_error = exc
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            last_validation_error = exc
        except Exception as exc:
            logger.error(
                "Azure OpenAI generate_stats non riuscito (%s).",
                type(exc).__name__,
            )
            raise RuntimeError("Chiamata Azure OpenAI per generate_stats non riuscita.") from exc

        logger.warning(
            "Azure OpenAI generate_stats: validazione fallita al tentativo %s/2 (%s).",
            attempt + 1,
            type(last_validation_error).__name__,
        )

    logger.error("Azure OpenAI generate_stats: risposta non valida dopo due tentativi.")
    raise RuntimeError(
        "Impossibile generare statistiche valide dopo due tentativi; verifica deployment e schema."
    ) from last_validation_error


def summarize(old_summary: str, new_messages: list[dict]) -> str:
    """Aggiorna il riassunto della storia senza inviare la cronologia completa."""
    system_prompt = (
        "Aggiorna il riassunto di una storia fantasy usando il riassunto precedente e i nuovi "
        "messaggi. Conserva eventi, decisioni dei giocatori, personaggi e dettagli utili alla "
        "continuità; non inventare fatti. Scrivi in italiano in massimo circa 200 parole."
    )
    context = {
        "riassunto_precedente": old_summary,
        "nuovi_messaggi": new_messages,
    }
    response = _create_completion(
        "summarize",
        model=_get_deployment("AZURE_OPENAI_STATS_DEPLOYMENT"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        max_tokens=_get_max_tokens(),
        temperature=0.2,
    )
    return _limit_words(_response_text(response, "summarize"), _SUMMARY_WORD_LIMIT)
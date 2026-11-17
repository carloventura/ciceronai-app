# CiceronAI

Bot Telegram per sessioni di gioco condivise, basato su Azure Functions,
Azure Storage Tables e Azure OpenAI.

## Flusso

`ChatMessage` valida il secret header di Telegram e il payload, accoda l'update
su Azure Storage Queue e risponde `200`. La logica applicativa viene eseguita
in modo asincrono da `ProcessTelegramUpdate`, che chiama
`process_telegram_update` e mantiene la macchina a stati Telegram esistente.

Il nome della coda si configura con `TELEGRAM_UPDATES_QUEUE` e predefinito è
`telegram-updates`. Gli update elaborati e i claim degli invii Telegram sono
registrati nella tabella `ProcessedUpdates`; la funzione di pulizia li elimina
insieme alle sessioni dopo `CHAT_RETENTION_DAYS`.

Le sessioni passano da `active` (raccolta giocatori) a `started` (Master in
esecuzione) e infine a `finished`. Alla registrazione il bot genera e mostra le
statistiche del personaggio. All'avvio il Master crea l'incipit; le azioni dei
giocatori ricevono risposte narrative. Sono mantenuti gli ultimi sei messaggi e
i precedenti vengono compressi nel riassunto. Ogni giocatore può inviare
un'azione ogni cinque secondi.

`MAX_TURNS_PER_SESSION` imposta il limite dei turni (predefinito `50`, minimo
`1`). L'ultimo turno chiede al Master un epilogo e porta la sessione a `finished`.

L'accesso è riservato: un nuovo Telegram ID viene salvato come `pending` e
l'amministratore riceve i pulsanti per approvare o rifiutare la richiesta. Da
`/admin` l'amministratore può consultare richieste e utenti, approvare,
rifiutare o revocare l'accesso e mettere in pausa/riprendere il gioco per tutti
gli utenti non-admin. La pausa non disabilita il pannello admin.

La queue trigger riprova un messaggio fino a tre tentativi. Dopo l'ultimo
fallimento Azure Functions lo sposta nella poison queue `<nome-coda>-poison`
(predefinita `telegram-updates-poison`).

I claim per messaggio evitano che un retry invii di nuovo lo stesso messaggio.
Telegram non offre una chiave d'idempotenza per `sendMessage`: un crash tra il
claim e la risposta di Telegram può quindi impedire il reinvio di quel messaggio,
perché non è possibile determinare con certezza se Telegram l'abbia accettato.

## Dati e AI

Ogni sessione è un'entità Azure Table. La proprietà `characters` contiene una
stringa JSON con personaggi, statistiche e ID Telegram. `master.py` espone
`narrate`, `generate_stats` e `summarize`; usa Azure OpenAI v1 con Managed
Identity in Azure o l'identità di sviluppo locale autenticata con `az login`.
Le proprietà `summary` e `recentMessages` sono entrambe stringhe JSON e vengono
controllate per restare sotto il limite di dimensione delle proprietà Azure Table.

## Configurazione

Installa le dipendenze dalla cartella Function App:

```sh
cd func-ciceronai-dev-szn
python -m pip install -r requirements.txt
```

Configura le seguenti variabili nelle Application Settings della Function App.
Per lo sviluppo locale, mettile in `local.settings.json` nella sezione `Values`
(il file non va versionato):

| Variabile | Obbligatoria | Descrizione |
| --- | --- | --- |
| `AzureWebJobsStorage` | Sì | Connessione usata dal queue trigger, dalla coda di output e da Azure Tables. |
| `FUNCTIONS_WORKER_RUNTIME` | Sì | Impostare a `python`. |
| `TELEGRAM_TOKEN` | Sì | Token del bot Telegram. |
| `TELEGRAM_WEBHOOK_SECRET` | Sì | Valore atteso nell'header `X-Telegram-Bot-Api-Secret-Token`. |
| `TELEGRAM_ADMIN_ID` | Sì | ID numerico Telegram dell'unico amministratore autorizzato a usare `/admin`. |
| `TELEGRAM_UPDATES_QUEUE` | No | Nome coda; predefinito `telegram-updates`. |
| `CHAT_SESSIONS_TABLE_NAME` | No | Tabella sessioni; predefinita `ChatSessions`. |
| `CHAT_RETENTION_DAYS` | No | Retention di sessioni e update; predefinita `1`, minimo `1`. |
| `MAX_TURNS_PER_SESSION` | No | Turni massimi per sessione; predefinito `50`, minimo `1`. |
| `AZURE_STORAGE_ACCOUNT_NAME` | Fallback | Account usato per Tables se `AzureWebJobsStorage` non è una connection string. |
| `AZURE_STORAGE_ACCOUNT_KEY` | Fallback | Chiave dell'account usata insieme al nome. |
| `AZURE_OPENAI_ENDPOINT` | AI | Endpoint della risorsa Azure OpenAI. |
| `AZURE_OPENAI_NARRATOR_DEPLOYMENT` | AI | Deployment per la narrazione. |
| `AZURE_OPENAI_STATS_DEPLOYMENT` | AI | Deployment economico per statistiche e riassunti. |
| `AZURE_OPENAI_MAX_TOKENS` | No | Limite output AI; predefinito `400`. |

Per l'identità gestita in Azure assegna alla Function App il ruolo
`Cognitive Services OpenAI User` sulla risorsa Azure OpenAI. In locale esegui
`az login` con un account che ha il ruolo d'inferenza richiesto.

## Esecuzione e webhook

Avvia la Function App dalla cartella `func-ciceronai-dev-szn`:

```sh
func start
```

L'endpoint HTTP è `/api/ChatMessage`. Per configurare il webhook Telegram,
imposta `FUNCTION_URL` all'URL pubblico della Function App e usa lo stesso secret
presente in `TELEGRAM_WEBHOOK_SECRET`:

```sh
curl -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
	--data-urlencode "url=${FUNCTION_URL}/api/ChatMessage" \
	--data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

## Test locale con Azurite

1. Installa Azurite e avvia i servizi Queue e Table:

	 ```sh
	 mkdir -p .azurite
	 azurite --location .azurite --debug .azurite/debug.log
	 ```

2. Nel `local.settings.json` imposta almeno `AzureWebJobsStorage` a
	 `UseDevelopmentStorage=true`, `FUNCTIONS_WORKER_RUNTIME` a `python`,
	 `TELEGRAM_WEBHOOK_SECRET` a un secret locale e `TELEGRAM_TOKEN` a un token
	 formalmente valido. L'update di prova seguente non contiene un messaggio e
	 non invia nulla a Telegram.

3. Avvia `func start` e invia due volte lo stesso update. La risposta HTTP deve
	 essere `200`; il secondo update deve risultare già completato in
	 `ProcessedUpdates` e non essere rielaborato. Senza il secret corretto la
	 risposta deve essere `401`.

	 ```sh
	 curl -i http://localhost:7071/api/ChatMessage \
		 -H 'Content-Type: application/json' \
		 -H "X-Telegram-Bot-Api-Secret-Token: ${TELEGRAM_WEBHOOK_SECRET}" \
		 -d '{"update_id":123456789,"ignored_update":true}'
	 ```

Per una prova Telegram reale serve inoltre un bot di test e un URL pubblico
temporaneo (ad esempio un tunnel): Telegram non può inviare webhook a
`localhost`.

## Struttura

```text
func-ciceronai-dev-szn/
	fantasy_names.py  # Liste per i codici sessione
	function_app.py   # Webhook, queue worker, sessioni e cleanup
	master.py         # Client Azure OpenAI e prompt
	host.json         # Retry queue e configurazione Azure Functions
	requirements.txt  # Dipendenze Python
```

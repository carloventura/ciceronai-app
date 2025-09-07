# ciceronAI – Telegram Tourist Guide Bot

Bot Telegram che funge da guida turistica virtuale, basato su Azure OpenAI e distribuito come Azure Function App.

## Funzionalità principali
- Risponde a foto e testo, incrociando posizione utente.
- Restituisce descrizioni turistiche in stile guida esperta.
- Gestione errori e tono amichevole.

## Setup
1. Installa le dipendenze:
   ```bash
   pip install -r requirements.txt
   ```
2. Configura i parametri Azure OpenAI e Telegram Bot API in variabili d'ambiente.
3. Avvia come Azure Function App oppure localmente per test.

## Deployment su Azure
- Segui la documentazione Azure Functions per Python.
- Imposta i secrets (API keys) su Azure.

## Struttura progetto
Vedi commenti nei file per dettagli.

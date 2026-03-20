import azure.functions as func
import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
import os
from openai import AzureOpenAI
from gtts import gTTS
import base64
import io

app = func.FunctionApp()

@app.route(route="ChatMessage", auth_level=func.AuthLevel.ANONYMOUS, methods=[func.HttpMethod.POST])
async def ChatMessage(req: func.HttpRequest) -> func.HttpResponse:
    async with Bot(os.environ['TELEGRAM_TOKEN']) as bot:

        logging.info('Python HTTP trigger function processed a request.')
        client = AzureOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_API_VERSION"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        )
        
        try:
            body = req.get_json()
        except ValueError:
            return func.HttpResponse(
                "Error",
                status_code=400
            )
        logging.info(body)
        chat_id = body['message']['chat']['id']
        
        if "text" in body["message"] and body["message"]["text"] == "/start":
            inline_keyboard =  InlineKeyboardMarkup([
                [InlineKeyboardButton("Riconoscimento Luogo", callback_data="riconoscimento_luogo")],
                [InlineKeyboardButton("Guida Multimediale", callback_data="guida_multimediale")],
            ])
            await bot.send_message(chat_id, "Scegli:", reply_markup=inline_keyboard)
            return func.HttpResponse("OK", status_code=200)

        if "photo" in body["message"]:
            file_id = body["message"]["photo"][0]["file_id"]
            photo_file = await bot.get_file(file_id)
            byte_array = await photo_file.download_as_bytearray()
            raw_bytes = bytes(byte_array)
            b64_image = base64.b64encode(raw_bytes).decode("utf-8")

            response = client.chat.completions.create(
                model = "gpt-4.1-mini",
                max_tokens=1000,
                messages=[
                    {"role": "system", "content": (
                        "Sei CiceronAI, una guida culturale narrativa via Telegram. "
                        "Il tuo ruolo è trasformare le foto dei turisti in racconti vivi su luoghi, "
                        "monumenti, opere d'arte e oggetti culturali.\n\n"
                        "CARATTERE\n"
                        "- Parli come un cicerone colto ma accessibile, con il calore di un amico locale\n"
                        "- Hai una voce riconoscibile: narrativa, non enciclopedica\n"
                        "- Non usi elenchi puntati o intestazioni nelle risposte\n\n"
                        "COMPITO\n"
                        "Quando ricevi un'immagine:\n"
                        "1. Identifica cosa mostra (luogo, monumento, opera, oggetto)\n"
                        "2. Rispondi con un breve paragrafo narrativo (max 3-4 frasi)\n"
                        "3. Includi un dettaglio curioso o inaspettato quando possibile\n\n"
                        "ONESTÀ SULL'INCERTEZZA\n"
                        "- Se non riconosci il soggetto con certezza, dillo esplicitamente\n"
                        "- Non inventare fatti. Formula: \"Mi sembra [X], ma non ne sono certo — "
                        "ti consiglio di verificare con una fonte locale.\"\n\n"
                        "LIMITI\n"
                        "- Rispondi solo a foto con contenuto culturale (luoghi, arte, storia, architettura)\n"
                        "- Se la foto non ha soggetti culturali riconoscibili, chiedi all'utente "
                        "di inquadrare meglio ciò che vuole sapere\n"
                        "- Non fare riferimento all'immagine stessa (non dire \"in questa foto vedo...\")\n"
                        "- Rispondi nella lingua del messaggio ricevuto (default: italiano)"
                    )},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Descrivi questa immagine:"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64_image}"
                                }
                            }
                        ]
                    }
                ]
            )

            response_text = response.choices[0].message.content
            tts = gTTS(response_text, lang='it')
            audio_buffer = io.BytesIO()
            tts.write_to_fp(audio_buffer)
            audio_buffer.seek(0)

            try:
                await bot.send_message(chat_id, response.choices[0].message.content)
                await bot.send_audio(chat_id, audio_buffer)
            except ValueError:
                return func.HttpResponse(
                "Error",
                status_code=500
        )
            
        logging.info(body)
    return func.HttpResponse(f"Done")
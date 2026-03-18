import azure.functions as func
import logging
from telegram import Bot
import os
from openai import AzureOpenAI
import base64

app = func.FunctionApp()

@app.route(route="ChatMessage", auth_level=func.AuthLevel.ANONYMOUS, methods=[func.HttpMethod.POST])
async def ChatMessage(req: func.HttpRequest) -> func.HttpResponse:
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
    


    async with Bot(os.environ['TELEGRAM_TOKEN']) as bot:
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
                    {"role": "system", "content": "Sei un assistente utile."},
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

            chat_id = body['message']['chat']['id']
            try:
                await bot.send_message(chat_id, response.choices[0].message.content)
            except ValueError:
                return func.HttpResponse(
                "Error",
                status_code=500
        )
            
        logging.info(body)
    return func.HttpResponse(f"Done")
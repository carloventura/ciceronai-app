import os
import requests

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

async def describe_place(place_info):
    prompt = f"Sei una guida turistica esperta. Descrivi il luogo seguente in modo amichevole e informativo: {place_info}"
    headers = {
        "api-key": AZURE_OPENAI_KEY,
        "Content-Type": "application/json"
    }
    data = {
        "prompt": prompt,
        "max_tokens": 300
    }
    url = f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}/completions?api-version=2022-12-01"
    response = requests.post(url, headers=headers, json=data)
    if response.ok:
        return response.json()["choices"][0]["text"].strip()
    return "Non sono riuscito a generare la descrizione. Riprova più tardi."

async def recognize_place_from_image(photo_file, coords):
    # Placeholder: Integrazione con Azure OpenAI Vision/Image
    # Si può usare un modello vision se disponibile
    return None

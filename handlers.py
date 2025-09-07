import logging
from services.telegram import send_message, get_user_location, get_photo_file
from services.azure_openai import describe_place, recognize_place_from_image
from utils.geo import get_place_from_coords

async def handle_update(update):
    try:
        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        photo = message.get("photo")
        location = message.get("location")

        # Gestione posizione
        if location:
            coords = (location["latitude"], location["longitude"])
        else:
            coords = await get_user_location(chat_id)
            if not coords:
                await send_message(chat_id, "Per favore, condividi la tua posizione per ricevere informazioni sul luogo.")
                return {"status": "waiting_location"}

        # Gestione foto
        if photo:
            photo_file = await get_photo_file(photo)
            place_name = await recognize_place_from_image(photo_file, coords)
            if not place_name:
                await send_message(chat_id, "Non sono riuscito a riconoscere il luogo dalla foto. Puoi riprovare con una foto più chiara o fornire una descrizione?")
                return {"status": "error_photo"}
        else:
            place_name = text

        # Incrocia posizione e nome/descrizione
        place_info = await get_place_from_coords(coords, place_name)
        description = await describe_place(place_info)
        await send_message(chat_id, description)
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Errore handler: {e}")
        return {"error": "Errore interno"}

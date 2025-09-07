import logging
import os
from azure.functions import HttpRequest, HttpResponse
from handlers import handle_update

async def main(req: HttpRequest) -> HttpResponse:
    try:
        data = req.get_json()
        response = await handle_update(data)
        return HttpResponse(str(response), mimetype="application/json")
    except Exception as e:
        logging.error(f"Errore nel webhook: {e}")
        return HttpResponse("{\"error\": \"Errore interno\"}", status_code=500)

# Per sviluppo locale (opzionale)
if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI, Request
    app = FastAPI()
    @app.post("/webhook")
    async def webhook(request: Request):
        data = await request.json()
        return await handle_update(data)
    uvicorn.run(app, host="0.0.0.0", port=8080)

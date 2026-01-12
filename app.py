from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
async def root():
    return {"mensagem": "Servidor FastAPI no Render está rodando!"}

@app.post("/webhook/kommo")
async def kommo_webhook(request: Request):
    data = await request.json()
    print("Webhook recebido do Kommo:", data)

    # Aqui depois vamos:
    # 1) extrair chat_id e mensagem
    # 2) chamar a IA
    # 3) enviar resposta pro Kommo
    return {"status": "ok"}

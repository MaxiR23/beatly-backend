from fastapi import FastAPI

app = FastAPI(title="Beatly API")

@app.get("/health")
def health():
    return {"status": "ok"}

import os
import httpx

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

app = FastAPI(title="A2Z Voice AI Studio")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"status": "running", "app": "A2Z Voice AI Studio"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/generate")
async def generate(
    lyrics: str = Form(...),
    style: str = Form(""),
    model_label: str = Form(""),
    voice_sample: UploadFile = File(...),
    reference_audio: UploadFile = File(None),
):
    model_url = os.getenv("MODEL_SERVER_URL", "").strip()

    if not model_url:
        raise HTTPException(
            status_code=503,
            detail="MODEL_SERVER_URL has not been configured."
        )

    fields = {
        "lyrics": lyrics,
        "style": style,
        "model_label": model_label,
    }

    files = []

    for name, upload in [
        ("voice_sample", voice_sample),
        ("reference_audio", reference_audio),
    ]:
        if upload:
            data = await upload.read()
            if data:
                files.append((
                    name,
                    (
                        upload.filename or "audio.wav",
                        data,
                        upload.content_type or "audio/wav",
                    ),
                ))

    headers = {}
    token = os.getenv("MODEL_SERVER_TOKEN", "").strip()

    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            result = await client.post(
                model_url,
                data=fields,
                files=files,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach singing model: {exc}",
        )

    if result.is_error:
        raise HTTPException(
            status_code=502,
            detail=f"Singing model error: {result.text[:1000]}",
        )

    content_type = result.headers.get(
        "content-type", "application/octet-stream"
    )

    return Response(
        content=result.content,
        status_code=result.status_code,
        media_type=content_type,
    )

fastapi
uvicorn[standard]
httpx
python-multipart


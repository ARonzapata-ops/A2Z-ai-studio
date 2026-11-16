"""A2Z Voice AI Studio backend adapter for the official SoulX-Singer Space.

The public Space's actual flow is:
1. Transcribe the reference voice audio to prompt metadata.
2. Transcribe the target vocal/melody guide to target metadata.
3. Call singing synthesis with prompt audio + both metadata files.

Typed lyrics and free-form style prompts are not direct inputs to this model's
synthesis function. The target guide determines the lyrics/melody that are sung.
"""

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from gradio_client import Client, handle_file

APP_TITLE = "A2Z Voice AI Studio"
SOULX_SPACE = os.getenv("SOULX_SPACE", "Soul-AILab/SoulX-Singer").strip()

app = FastAPI(title=APP_TITLE)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-A2Z-Model", "X-A2Z-Lyrics-Source"],
)

_client: Client | None = None
_api_names: dict[str, str] = {}


def get_client() -> Client:
    """Create one Gradio client so the API calls share a client session."""
    global _client
    if _client is None:
        _client = Client(SOULX_SPACE, verbose=False)
    return _client


def _endpoint(client: Client, key: str, candidates: tuple[str, ...]) -> str:
    """Resolve endpoint names against the Space API schema, with safe fallbacks."""
    if key in _api_names:
        return _api_names[key]
    try:
        schema = client.view_api(print_info=False, return_format="dict") or {}
        named = schema.get("named_endpoints", {}) if isinstance(schema, dict) else {}
        # Gradio versions have used both slash-prefixed and bare names.
        normalized = {str(name).lstrip("/"): str(name) for name in named}
        for candidate in candidates:
            if candidate.lstrip("/") in normalized:
                _api_names[key] = normalized[candidate.lstrip("/")]
                return _api_names[key]
    except Exception:
        # Let the actual request produce a useful error if schema discovery is unavailable.
        pass
    _api_names[key] = candidates[0]
    return _api_names[key]


def _first_output(result: Any, label: str) -> Any:
    if isinstance(result, (tuple, list)):
        if not result:
            raise RuntimeError(f"SoulX-Singer returned no {label} outputs.")
        return result[0]
    return result


def _file_path(value: Any, label: str) -> str:
    """Extract a downloaded local filepath from Gradio's file output value."""
    if isinstance(value, dict):
        value = value.get("path") or value.get("name") or value.get("url")
    elif isinstance(value, (tuple, list)):
        value = value[0] if value else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"SoulX-Singer did not return {label}.")
    if value.startswith(("http://", "https://")):
        return value
    path = Path(value)
    if not path.is_file():
        raise RuntimeError(f"SoulX-Singer returned a missing {label} file.")
    return str(path)


VOICE_PARAMETERS = (
    'prompt_audio', 'target_audio', 'prompt_metadata', 'target_metadata',
    'control', 'auto_shift', 'pitch_shift', 'seed', 'prompt_lyric_lang',
    'target_lyric_lang', 'prompt_vocal_sep', 'target_vocal_sep',
)


def voice_endpoint(client):
    """Verify the provider's current, public API contract before sending audio."""
    schema = client.view_api(print_info=False, return_format="dict") or {}
    named = schema.get('named_endpoints', {})
    for name, endpoint in named.items():
        parameters = tuple(p.get('parameter_name') for p in endpoint.get('parameters', []))
        if parameters == VOICE_PARAMETERS:
            return name
    raise RuntimeError('The singing service API has changed. No audio was sent. The A2Z adapter needs an update.')


def _call_soulx(prompt_path: str, target_path: str) -> Any:
    # Use an isolated session for each job. The current Space exposes one combined
    # synthesis_function endpoint, with transcription performed by the provider.
    client = Client(SOULX_SPACE, verbose=False)
    endpoint = voice_endpoint(client)
    return client.predict(
        handle_file(prompt_path), handle_file(target_path), None, None,
        'melody', True, 0, 12306, 'English', 'English', False, True,
        api_name=endpoint,
    )


@app.get('/api/voice/capabilities')
async def voice_capabilities():
    try:
        endpoint = await run_in_threadpool(lambda: voice_endpoint(get_client()))
        return {'reachable': True, 'api_compatible': True, 'endpoint': endpoint,
                'requires_voice_sample': True, 'requires_audio_guide': True,
                'generation_tested': False,
                'message': 'Live singing API checked. Generation still depends on the provider queue and uploaded audio.'}
    except Exception:
        return JSONResponse({'reachable': False, 'api_compatible': False,
                             'generation_tested': False,
                             'message': 'The singing service could not be verified. Your drafts are safe; try the connection check later.'}, status_code=503)


@app.get("/")
def home():
    return {
        "status": "running",
        "app": APP_TITLE,
        "singing_model": SOULX_SPACE,
        "generation_route": "/generate",
        "generation_flow": "combined guide transcription and synthesis",
    }


@app.get("/health")
def health():
    return {"ok": True, "app": APP_TITLE, "model_configured": bool(SOULX_SPACE)}


@app.post("/generate")
async def generate(
    lyrics: str = Form(""),
    style: str = Form(""),
    model_label: str = Form(""),
    creation_mode: str = Form("song"),
    studio_mode: str = Form("false"),
    genre: str = Form(""),
    vocal_delivery: str = Form(""),
    mood: str = Form(""),
    tempo: str = Form(""),
    voice_sample: UploadFile = File(...),
    reference_audio: UploadFile | None = File(None),
):
    # SoulX-Singer's synthesis model uses the guide audio's detected lyrics and
    # melody. These form fields are accepted for frontend compatibility but are
    # not direct controls in the official Space's synthesis callback.
    if reference_audio is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Select a target vocal/melody guide (up to 60 seconds). SoulX-Singer "
                "needs both the reference voice sample and the target guide."
            ),
        )

    max_upload = 25 * 1024 * 1024
    prompt_bytes = await voice_sample.read(max_upload + 1)
    target_bytes = await reference_audio.read(max_upload + 1)
    if len(prompt_bytes) > max_upload or len(target_bytes) > max_upload:
        raise HTTPException(status_code=413, detail="Each voice recording must be 25 MB or smaller.")
    if not prompt_bytes:
        raise HTTPException(status_code=400, detail="The reference voice sample is empty.")
    if not target_bytes:
        raise HTTPException(status_code=400, detail="The target vocal/melody guide is empty.")

    prompt_suffix = Path(voice_sample.filename or "voice.wav").suffix or ".wav"
    target_suffix = Path(reference_audio.filename or "guide.wav").suffix or ".wav"

    try:
        with tempfile.TemporaryDirectory(prefix="a2z-soulx-") as temp_dir:
            prompt_path = str(Path(temp_dir) / f"prompt{prompt_suffix}")
            target_path = str(Path(temp_dir) / f"target{target_suffix}")
            Path(prompt_path).write_bytes(prompt_bytes)
            Path(target_path).write_bytes(target_bytes)

            try:
                result = await run_in_threadpool(_call_soulx, prompt_path, target_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "SoulX-Singer generation failed during transcription or synthesis. "
                        "The public Space may be queued, starting, or its API may have changed. "
                        f"Details: {str(exc)[:700]}"
                    ),
                ) from exc

            output_path = _file_path(_first_output(result, "generated-audio"), "generated-audio")
            if output_path.startswith(("http://", "https://")):
                import httpx
                async with httpx.AsyncClient(timeout=120) as client:
                    audio_response = await client.get(output_path)
                if audio_response.is_error:
                    raise HTTPException(status_code=502, detail="Could not download generated audio from SoulX-Singer.")
                audio_bytes = audio_response.content
                media_type = audio_response.headers.get("content-type", "audio/wav")
            else:
                audio_bytes = Path(output_path).read_bytes()
                media_type = "audio/wav" if Path(output_path).suffix.lower() == ".wav" else "application/octet-stream"

            if not audio_bytes:
                raise HTTPException(status_code=502, detail="SoulX-Singer returned an empty audio file.")

            return Response(
                content=audio_bytes,
                media_type=media_type,
                headers={
                    "X-A2Z-Model": SOULX_SPACE,
                    "X-A2Z-Lyrics-Source": "target-audio-guide",
                    "Cache-Control": "no-store",
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Generation failed: {str(exc)[:700]}") from exc



# A2Z Art: text-to-image and reference-guided editing.
# Provider credentials are server-only. Existing singing routes above are unchanged.
import asyncio
import base64
import binascii
import hashlib
import secrets
from fastapi import Request
from fastapi.responses import JSONResponse

ART_MAX_FILE = 10 * 1024 * 1024
ART_MAX_BODY = 32 * 1024 * 1024
_art_lock = asyncio.Lock()


def art_configuration():
    return {
        'key': os.getenv('A2Z_ART_API_KEY', '').strip(),
        'owner': os.getenv('A2Z_ART_OWNER_KEY', '').strip(),
        'model': os.getenv('A2Z_ART_MODEL', 'gpt-image-2.5-flare').strip(),
    }


@app.middleware('http')
async def protect_art_requests(request: Request, call_next):
    if request.url.path == '/api/art/images' and request.method == 'POST':
        cfg = art_configuration()
        if not cfg['owner'] or not cfg['key']:
            return JSONResponse({'detail': 'Art generation is not connected. Set A2Z_ART_API_KEY and A2Z_ART_OWNER_KEY on the server.'}, status_code=503, headers={'Access-Control-Allow-Origin': '*'})
        supplied = request.headers.get('x-a2z-art-key', '')
        if not supplied or not secrets.compare_digest(supplied, cfg['owner']):
            return JSONResponse({'detail': 'The art access key is missing or incorrect.'}, status_code=401, headers={'Access-Control-Allow-Origin': '*'})
        length = request.headers.get('content-length')
        if length:
            try:
                if int(length) > ART_MAX_BODY:
                    return JSONResponse({'detail': 'Upload at most three images, 10 MB each.'}, status_code=413, headers={'Access-Control-Allow-Origin': '*'})
            except ValueError:
                return JSONResponse({'detail': 'Invalid request size.'}, status_code=400, headers={'Access-Control-Allow-Origin': '*'})
    return await call_next(request)


@app.get('/api/art/capabilities')
def art_capabilities():
    cfg = art_configuration()
    return {
        'version': '1.7.2',
        'image_generation': bool(cfg['key'] and cfg['owner']),
        'reference_editing': bool(cfg['key'] and cfg['owner']),
        'access_key_required': True,
        'provider_access_verified': False,
        'generation_tested': False,
        'message': 'Server configuration only. Use the authenticated access check to verify provider access.',
        'max_reference_images': 3,
        'max_reference_bytes': ART_MAX_FILE,
        'model': cfg['model'],
        'video_generation': False,
    }


def image_mime(data: bytes):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


async def art_provider_request(prompt, size, quality, references, cfg):
    import httpx
    common = {'model': cfg['model'], 'prompt': prompt, 'size': size,
              'quality': quality, 'n': 1, 'output_format': 'png',
              'safety_identifier': hashlib.sha256(cfg['owner'].encode()).hexdigest()}
    headers = {'Authorization': 'Bearer ' + cfg['key']}
    async with httpx.AsyncClient(timeout=240, follow_redirects=False) as client:
        if references:
            form = {k: str(v) for k, v in common.items()}
            files = [('image[]', (f'reference-{i+1}.{mime.split("/")[1]}', data, mime))
                     for i, (data, mime) in enumerate(references)]
            result = await client.post('https://api.openai.com/v1/images/edits', headers=headers, data=form, files=files)
        else:
            result = await client.post('https://api.openai.com/v1/images/generations', headers=headers, json=common)
        if result.status_code >= 400:
            if result.status_code in (401, 403):
                raise HTTPException(502, 'The image provider rejected the server credentials or model access. Check the server setup.')
            if result.status_code == 429:
                raise HTTPException(429, 'The image provider is at its quota or rate limit. No automatic retry was made.')
            try:
                message = str(result.json().get('error', {}).get('message', 'Image provider rejected the request.'))
            except (ValueError, AttributeError):
                message = 'Image provider rejected the request.'
            raise HTTPException(502, message.replace(cfg['key'], '[redacted]')[:300])
        try:
            encoded = result.json()['data'][0]['b64_json']
            if not isinstance(encoded, str) or len(encoded) > 50_000_000:
                raise ValueError('Invalid image payload')
            data = base64.b64decode(encoded, validate=True)
            if image_mime(data) != 'image/png':
                raise ValueError('Not a PNG result')
            return data
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error):
            raise HTTPException(502, 'The image provider returned no usable PNG image.')


@app.post('/api/art/images')
async def generate_art(
    prompt: str = Form(..., min_length=1, max_length=20000),
    size: str = Form('1024x1024'),
    quality: str = Form('high'),
    references: list[UploadFile] | None = File(None),
):
    if not prompt.strip():
        raise HTTPException(422, 'Describe the artwork first.')
    if size not in {'1024x1024', '1024x1536', '1536x1024'} or quality not in {'medium', 'high'}:
        raise HTTPException(422, 'Unsupported size or quality.')
    if len(references or []) > 3:
        raise HTTPException(422, 'Choose no more than three reference images.')
    images = []
    for upload in references or []:
        data = await upload.read(ART_MAX_FILE + 1)
        if not data or len(data) > ART_MAX_FILE:
            raise HTTPException(413, 'Each reference must be a nonempty image, 10 MB or smaller.')
        mime = image_mime(data)
        if mime is None:
            raise HTTPException(415, 'Reference images must be PNG, JPEG, or WebP. Convert HEIC photos before uploading.')
        images.append((data, mime))
    if _art_lock.locked():
        raise HTTPException(429, 'An image is already generating. Wait for it to finish before starting another.')
    async with _art_lock:
        try:
            data = await art_provider_request(prompt.strip(), size, quality, images, art_configuration())
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, 'The image service did not finish normally. Check its request history before retrying; the app did not automatically retry.')
    return Response(data, media_type='image/png', headers={'Cache-Control': 'no-store', 'Content-Disposition': 'inline; filename="a2z-art.png"'})

async def verify_art_model(cfg):
    import httpx
    from urllib.parse import quote
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get('https://api.openai.com/v1/models/' + quote(cfg['model'], safe=''),
                                        headers={'Authorization': 'Bearer ' + cfg['key']})
        if response.status_code != 200:
            try:
                error = response.json().get('error', {})
                code = error.get('code') if isinstance(error, dict) else None
            except (ValueError, AttributeError):
                code = None
            reasons = {
                'invalid_api_key': 'The provider API key is invalid. Replace A2Z_ART_API_KEY on the server.',
                'insufficient_quota': 'The provider account has no available quota. Check API billing and project limits.',
                'model_not_found': 'The configured image model is unavailable to this provider account.',
            }
            reason = reasons.get(str(code), {
                401: 'The provider rejected the server API key.',
                403: 'The provider denied model access. Check project permissions and account verification.',
                404: 'The configured image model was not found or is unavailable to this account.',
                429: 'The provider reported a quota or rate limit.',
            }.get(response.status_code, 'The provider could not verify model access.'))
            raise HTTPException(502, f'{reason} Provider HTTP {response.status_code}. No image was generated.')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, 'The image provider could not be reached to check model access.')


@app.get('/api/art/access')
async def check_art_access(request: Request):
    cfg = art_configuration()
    if not cfg['key'] or not cfg['owner']:
        raise HTTPException(503, 'Art generation is not configured on this server yet.')
    supplied = request.headers.get('x-a2z-art-key', '')
    if not supplied or not secrets.compare_digest(supplied, cfg['owner']):
        raise HTTPException(401, 'The art access key is missing or incorrect.')
    await verify_art_model(cfg)
    return {'connected': True, 'model': cfg['model'], 'message': 'Server access and image-model access checked. No image was generated.'}

# First self-hosted A2Z processing engine: owner-only queued photo/audio rendering.
import workhorse
workhorse.install(app)

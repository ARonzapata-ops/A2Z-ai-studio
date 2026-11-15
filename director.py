"""Prompt -> reviewed storyboard -> bounded Runway footage -> one MP4."""
import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

router = APIRouter()
_script_lock = threading.Lock()
_engine = None


def script_key():
    return os.getenv('A2Z_SCRIPT_API_KEY', '').strip() or os.getenv('A2Z_ART_API_KEY', '').strip()


def runway_key():
    return os.getenv('RUNWAYML_API_SECRET', '').strip()


class Brief(BaseModel):
    idea: str = Field(min_length=10, max_length=12000)
    style: str = Field(default='Cinematic, emotionally expressive', max_length=500)
    scenes: int = Field(default=3, ge=1, le=6)
    format: str = Field(default='vertical', pattern='^(vertical|landscape)$')


class Scene(BaseModel):
    description: str = Field(min_length=1, max_length=1500)
    narration: str = Field(default='', max_length=600)
    prompt: str = Field(min_length=1, max_length=900)


class Plan(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    continuity: str = Field(max_length=1500)
    scenes: list[Scene] = Field(min_length=1, max_length=6)


def require_ok(response, provider):
    if response.status_code >= 400:
        raise RuntimeError(f'{provider} returned HTTP {response.status_code}. Check account access, credits and request history. No automatic generation retry was made.')
    return response.json()


def schema():
    string = {'type': 'string'}
    return {'type': 'object', 'additionalProperties': False,
            'properties': {'title': string, 'continuity': string,
                           'scenes': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                                       'properties': {'description': string, 'narration': string, 'prompt': string},
                                       'required': ['description', 'narration', 'prompt']}}},
            'required': ['title', 'continuity', 'scenes']}


def make_plan(brief):
    if not script_key():
        raise HTTPException(503, 'Connect a script API key on the server first.')
    if not _script_lock.acquire(blocking=False):
        raise HTTPException(429, 'A script is already being written. Wait for it to finish.')
    try:
        instructions = ('You are the A2Z film director. Develop the user idea into a coherent story with an opening, progression and ending. '
                        f'Produce exactly {brief.scenes} scenes, each FIVE seconds. Keep narration short enough for five seconds. '
                        'Continuity describes recurring characters and a shared visual style; repeat essential character details inside EVERY prompt. '
                        'Each prompt must stand alone, under 850 characters, describe one achievable shot, subject action, camera movement and lighting. '
                        'Preserve user meaning and requested tone. Do not include subtitles, logos or on-screen text in footage. '
                        'Do not claim exact real-person likeness or guaranteed character consistency. Return only the requested schema.')
        with httpx.Client(timeout=100) as client:
            response = client.post('https://api.openai.com/v1/responses',
                                   headers={'Authorization': 'Bearer ' + script_key()},
                                   json={'model': os.getenv('A2Z_SCRIPT_MODEL', 'gpt-4o-mini'), 'store': False,
                                         'instructions': instructions,
                                         'input': json.dumps(brief.model_dump()), 'max_output_tokens': 5000,
                                         'text': {'format': {'type': 'json_schema', 'name': 'video_plan', 'strict': True, 'schema': schema()}}})
        body = require_ok(response, 'Script provider')
        if body.get('status') != 'completed':
            raise RuntimeError('The writer did not complete the script. No footage was requested.')
        text = ''.join(c.get('text', '') for item in body.get('output', []) for c in item.get('content', []) if c.get('type') == 'output_text')
        plan = Plan.model_validate_json(text)
        if len(plan.scenes) != brief.scenes:
            raise RuntimeError('The script returned the wrong scene count. No footage was requested.')
        identifier = str(uuid.uuid4())
        if any(len(scene.prompt.encode('utf-16-le')) // 2 > 1000 for scene in plan.scenes):
            raise RuntimeError('A scene prompt exceeded the footage provider limit. No footage was requested.')
        directory = _engine.ROOT / identifier
        directory.mkdir(parents=True, mode=0o700)
        (directory / 'plan.json').write_text(json.dumps({'format': brief.format, 'plan': plan.model_dump()}))
        with _engine.database() as db:
            db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)', (identifier, 'storyboard', 0, time.time(), 'Ready for review'))
        return {'id': identifier, 'plan': plan.model_dump(), 'seconds': brief.scenes * 5, 'format': brief.format}
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from None
    except Exception:
        raise HTTPException(502, 'Script writing did not finish. Check provider request history before trying again. No footage was requested.') from None
    finally:
        _script_lock.release()


def render(identifier, spec):
    e = _engine
    directory = e.ROOT / identifier
    scenes = spec['plan']['scenes']
    headers = {'Authorization': 'Bearer ' + runway_key(), 'X-Runway-Version': '2024-11-06'}
    size = '720:1280' if spec['format'] == 'vertical' else '1280:720'
    width, height = size.split(':')
    with httpx.Client(timeout=60) as client:
        for i, scene in enumerate(scenes):
            e.update(identifier, 'running', int(i / len(scenes) * 85), f'Generating moving scene {i+1} of {len(scenes)}…')
            # A checkpoint precedes the paid POST: ambiguous responses never trigger a second POST.
            checkpoint = directory / f'task-{i}.json'
            if checkpoint.exists():
                task_id = json.loads(checkpoint.read_text()).get('id')
                if not task_id:
                    raise RuntimeError('A footage request has uncertain status. Check Runway history; automatic resubmission is disabled.')
            else:
                checkpoint.write_text('{}')
                task = require_ok(client.post('https://api.dev.runwayml.com/v1/text_to_video', headers=headers,
                                             json={'model': 'gen4.5', 'promptText': scene['prompt'], 'ratio': size, 'duration': 5}), 'Runway')
                task_id = task['id']
                checkpoint.write_text(json.dumps({'id': task_id}))
            deadline = time.monotonic() + 1200
            while True:
                if e._stop.is_set():
                    raise RuntimeError('Server restarted. Check Runway history before creating another video.')
                task = require_ok(client.get('https://api.dev.runwayml.com/v1/tasks/' + task_id, headers=headers), 'Runway')
                state = task.get('status')
                if state == 'SUCCEEDED':
                    break
                if state in {'FAILED', 'CANCELLED'}:
                    raise RuntimeError(f'Runway scene {i+1} {state.lower()}. Completed requests may be billable; no automatic retry was made.')
                if time.monotonic() > deadline:
                    raise RuntimeError('Footage is taking longer than expected. Check Runway history. It has not been resubmitted.')
                if e._stop.wait(5):
                    raise RuntimeError('Server stopped while waiting for footage.')
            output = task.get('output', [])
            if not output or urlsplit(output[0]).scheme != 'https':
                raise RuntimeError('Runway returned no downloadable footage.')
            clip = directory / f'clip-{i}.mp4'
            with client.stream('GET', output[0]) as response:
                response.raise_for_status()
                total = 0
                with clip.open('wb') as target:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > 150 * 1024 * 1024:
                            raise RuntimeError('Generated footage exceeds the download limit.')
                        target.write(chunk)
            e.command(['-i', str(clip), '-an', '-t', '5', '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p', '-c:v', 'libx264', '-preset', 'veryfast', '-threads', '1', str(directory / f'scene-{i}.mp4')], directory)
            clip.unlink(missing_ok=True)
    playlist = directory / 'scenes.txt'
    playlist.write_text(''.join(f"file 'scene-{i}.mp4'\n" for i in range(len(scenes))))
    args = ['-f', 'concat', '-safe', '1', '-i', str(playlist)]
    if (directory / 'audio').exists():
        args += ['-protocol_whitelist', 'file,pipe', '-format_whitelist', 'wav,mp3,mov,ogg,flac,aac,matroska,webm', '-i', str(directory / 'audio'), '-map', '0:v:0', '-map', '1:a:0', '-c:a', 'aac', '-af', 'apad']
    else:
        args += ['-an']
    e.update(identifier, 'running', 95, 'Assembling your finished video…')
    e.command([*args, '-c:v', 'copy', '-t', str(len(scenes) * 5), '-movflags', '+faststart', str(directory / 'video.mp4')], directory)
    if not (directory / 'video.mp4').stat().st_size:
        raise RuntimeError('The output video is empty.')
    e.update(identifier, 'complete', 100)
    for path in directory.iterdir():
        if path.name not in {'video.mp4', 'plan.json'} and not path.name.startswith('task-'):
            path.unlink(missing_ok=True)


def install(app, engine):
    global _engine
    _engine = engine

    @router.get('/api/director/capabilities')
    def capabilities():
        return {'script_connected': bool(script_key()), 'video_connected': bool(runway_key()),
                'video_provider': 'Runway Gen-4.5', 'max_scenes': 6, 'seconds_per_scene': 5,
                'spoken_narration': False, 'character_references': False}

    @router.post('/api/director/plan', dependencies=[Depends(engine.owner)])
    def plan(brief: Brief):
        return make_plan(brief)

    @router.post('/api/director/render/{identifier}', dependencies=[Depends(engine.owner)], status_code=202)
    async def submit(identifier: str, plan: str = Form(..., max_length=30000), approved: bool = Form(False), audio: UploadFile | None = File(None)):
        identifier = engine.job_id(identifier)
        if not approved:
            raise HTTPException(422, 'Review the storyboard and approve the paid footage requests first.')
        if not runway_key() or not engine.ffmpeg():
            raise HTTPException(503, 'Connect RUNWAYML_API_SECRET and the video renderer on the server first.')
        try:
            reviewed = Plan.model_validate_json(plan)
        except ValueError:
            raise HTTPException(422, 'The edited storyboard is invalid.') from None
        if any(len(scene.prompt.encode('utf-16-le')) // 2 > 1000 for scene in reviewed.scenes):
            raise HTTPException(422, 'Each footage prompt must fit within 1000 UTF-16 characters.')
        directory = engine.ROOT / identifier
        if not (directory / 'plan.json').is_file():
            raise HTTPException(404, 'Storyboard expired. Create a new script.')
        # Reserve under a DB transaction before reading uploads or starting provider requests.
        with engine.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM jobs WHERE id=?', (identifier,)).fetchone()
            if not row or row['state'] != 'storyboard':
                raise HTTPException(409, 'This storyboard has already been submitted. Resume its status check instead.')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','uploading')").fetchone()[0] >= engine.MAX_PENDING:
                raise HTTPException(429, 'The video queue is full.')
            db.execute("UPDATE jobs SET state='uploading' WHERE id=?", (identifier,))
        try:
            original = json.loads((directory / 'plan.json').read_text())
            if len(reviewed.scenes) != len(original['plan']['scenes']):
                raise HTTPException(422, 'Keep the reviewed scene count unchanged.')
            if audio:
                total = 0
                with (directory / 'audio').open('wb') as target:
                    while chunk := await audio.read(1024 * 1024):
                        total += len(chunk)
                        if total > 50 * 1024 * 1024:
                            raise HTTPException(413, 'Soundtrack must be 50 MB or smaller.')
                        target.write(chunk)
                if not total:
                    raise HTTPException(422, 'The soundtrack is empty.')
            spec = {'kind': 'ai', 'format': original['format'], 'plan': reviewed.model_dump()}
            (directory / 'input.json').write_text(json.dumps(spec))
            (directory / 'plan.json').write_text(json.dumps({'format': original['format'], 'plan': reviewed.model_dump()}))
            engine.update(identifier, 'queued', 0, 'Footage queued')
            return {'id': identifier, 'state': 'queued'}
        except Exception:
            (directory / 'audio').unlink(missing_ok=True)
            engine.update(identifier, 'storyboard', 0, 'Review and submit again')
            raise

    app.include_router(router)

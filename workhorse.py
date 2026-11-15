"""A2Z's bounded, owner-only photo + audio video renderer (no AI API calls)."""
from contextlib import asynccontextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

router = APIRouter()
ROOT = Path(os.getenv('A2Z_WORKHORSE_DIR', '/tmp/a2z-workhorse'))
_stop = threading.Event()
_thread = None
log = logging.getLogger(__name__)
MAX_PENDING = 3
TTL = 86400


def database():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(ROOT / 'jobs.sqlite3', timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, progress INTEGER, created REAL, detail TEXT)')
    return db


def ffmpeg():
    executable = shutil.which('ffmpeg')
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


def owner(request: Request):
    expected = os.getenv('A2Z_ART_OWNER_KEY', '').strip()
    supplied = request.headers.get('X-A2Z-Art-Key', '')
    if not expected:
        raise HTTPException(503, 'Set the owner access password on the server first.')
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, 'The owner access password is missing or incorrect.')


def job_id(value):
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(422, 'Invalid job ID.')


def update(identifier, state, progress, detail=''):
    with database() as db:
        db.execute('UPDATE jobs SET state=?, progress=?, detail=? WHERE id=?', (state, progress, detail, identifier))


def command(arguments, directory):
    # No shell, no network inputs. One encoder thread keeps small servers usable.
    with (directory / 'render.log').open('wb') as errors:
        process = subprocess.Popen([ffmpeg(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-max_alloc', '67108864', '-y', *arguments], stdout=subprocess.DEVNULL, stderr=errors)
        deadline = time.monotonic() + 900
        try:
            while process.poll() is None:
                if _stop.wait(.25):
                    raise InterruptedError('Server restarting. Please submit again.')
                if time.monotonic() > deadline:
                    raise RuntimeError('Rendering took too long. Try a shorter video.')
            if process.returncode:
                raise RuntimeError('Could not render this media. Try a standard photo and MP3 or WAV audio.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def render(identifier):
    directory = ROOT / identifier
    spec = json.loads((directory / 'input.json').read_text())
    count = spec['count']
    seconds = spec['duration'] / count
    width, height = (720, 1280) if spec['format'] == 'vertical' else (1280, 720)
    for i in range(count):
        command(['-threads', '1', '-loop', '1', '-framerate', '24', '-i', str(directory / f'photo-{i}.jpg'),
                 '-t', str(seconds), '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p',
                 '-filter_threads', '1', '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-threads', '1', str(directory / f'scene-{i}.mp4')], directory)
        update(identifier, 'running', int((i + 1) / count * 85))
    playlist = directory / 'scenes.txt'
    playlist.write_text(''.join(f"file 'scene-{i}.mp4'\n" for i in range(count)))
    command(['-f', 'concat', '-safe', '1', '-i', str(playlist), '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'wav,mp3,mov,ogg,flac,aac,matroska,webm', '-i', str(directory / 'audio'),
             '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
             '-af', 'apad', '-t', str(spec['duration']), '-movflags', '+faststart', '-threads', '1', str(directory / 'video.mp4')], directory)
    if not (directory / 'video.mp4').stat().st_size:
        raise RuntimeError('The renderer returned an empty video.')
    update(identifier, 'complete', 100)
    # Retain only the deliverable, not original private uploads.
    for path in directory.iterdir():
        if path.name != 'video.mp4':
            path.unlink(missing_ok=True)


def worker():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # The process lock guarantees no other renderer owns these jobs.
        with database() as db:
            db.execute("UPDATE jobs SET state='failed', detail='Server restarted during rendering. Submit again.' WHERE state='running'")
        while not _stop.is_set():
            with database() as db:
                expired = db.execute('SELECT id FROM jobs WHERE created < ? AND state != ?', (time.time() - TTL, 'running')).fetchall()
                for row in expired:
                    shutil.rmtree(ROOT / row['id'], ignore_errors=True)
                    db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))
                row = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                identifier = row['id']
                update(identifier, 'running', 0)
                try:
                    render(identifier)
                except Exception as exc:
                    log.warning('Video job %s failed: %s', identifier, type(exc).__name__)
                    update(identifier, 'failed', 0, str(exc) if isinstance(exc, (RuntimeError, InterruptedError)) else 'Video processing failed. Submit again with standard media files.')
                    shutil.rmtree(ROOT / identifier, ignore_errors=True)
            else:
                _stop.wait(1)


def start():
    global _thread
    _stop.clear()
    with database():
        pass
    _thread = threading.Thread(target=worker, daemon=True, name='a2z-video-renderer')
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=10)


@asynccontextmanager
async def lifespan(app):
    start()
    try:
        yield
    finally:
        stop()


@router.get('/workhorse')
def page():
    return FileResponse(Path(__file__).with_name('workhorse.html'), headers={'Cache-Control': 'no-store'})


@router.get('/api/workhorse/capabilities')
def capabilities():
    return {'renderer_available': bool(ffmpeg()), 'owner_key_configured': bool(os.getenv('A2Z_ART_OWNER_KEY', '').strip()),
            'ai_video_generation': False, 'max_duration_seconds': 300, 'max_photos': 10,
            'retention_hours': 24, 'storage': 'server-local; survives restarts only with a persistent disk'}


@router.post('/api/workhorse/jobs', dependencies=[Depends(owner)], status_code=202)
async def submit(photos: list[UploadFile] = File(...), audio: UploadFile = File(...), duration: int = Form(60), format: str = Form('vertical')):
    if not ffmpeg():
        raise HTTPException(503, 'FFmpeg is not installed on this server.')
    if not 1 <= len(photos) <= 10 or not 5 <= duration <= 300 or format not in {'vertical', 'landscape'}:
        raise HTTPException(422, 'Use 1–10 photos, 5–300 seconds, and vertical or landscape format.')
    identifier = str(uuid.uuid4())
    directory = Path(tempfile.mkdtemp(prefix='upload-', dir=ROOT))
    try:
        for i, photo in enumerate(photos):
            data = await photo.read(5 * 1024 * 1024 + 1)
            if not data or len(data) > 5 * 1024 * 1024:
                raise HTTPException(413, 'Each photo must be between 1 byte and 5 MB.')
            source = directory / 'source'
            source.write_bytes(data)
            try:
                with Image.open(source) as image:
                    if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > 20_000_000:
                        raise ValueError('unsupported image')
                    from PIL import ImageOps
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((1920, 1920))
                    image.convert('RGB').save(directory / f'photo-{i}.jpg')
            except Exception:
                raise HTTPException(415, 'Use PNG, JPEG or WebP photos under 20 megapixels. Convert HEIC first.')
            source.unlink()
        total = 0
        with (directory / 'audio').open('wb') as target:
            while chunk := await audio.read(1024 * 1024):
                total += len(chunk)
                if total > 50 * 1024 * 1024:
                    raise HTTPException(413, 'Audio must be 50 MB or smaller.')
                target.write(chunk)
        if not total:
            raise HTTPException(422, 'Choose a nonempty audio file.')
        (directory / 'input.json').write_text(json.dumps({'count': len(photos), 'duration': duration, 'format': format}))
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= MAX_PENDING:
                raise HTTPException(429, 'The video queue is full. Wait for a job to finish.')
            directory.rename(ROOT / identifier)
            db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)', (identifier, 'queued', 0, time.time(), ''))
        return {'id': identifier, 'state': 'queued'}
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@router.get('/api/workhorse/jobs/{identifier}', dependencies=[Depends(owner)])
def status(identifier: str):
    with database() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id(identifier),)).fetchone()
    if row is None:
        raise HTTPException(404, 'Job not found or expired.')
    return dict(row)


@router.get('/api/workhorse/jobs/{identifier}/video', dependencies=[Depends(owner)])
def download(identifier: str):
    job = status(identifier)
    path = ROOT / job['id'] / 'video.mp4'
    if job['state'] != 'complete' or not path.is_file():
        raise HTTPException(409, 'The video is not ready or is no longer stored.')
    return FileResponse(path, media_type='video/mp4', filename='a2z-video.mp4', headers={'Cache-Control': 'no-store'})


def install(app):
    app.include_router(router)
    app.router.lifespan_context = lifespan

    @app.middleware('http')
    async def protect_upload(request, call_next):
        if request.url.path == '/api/workhorse/jobs' and request.method == 'POST':
            try:
                owner(request)
                length = request.headers.get('content-length')
                if length is None:
                    raise HTTPException(411, 'An upload content length is required.')
                try:
                    size = int(length)
                except ValueError:
                    raise HTTPException(400, 'Invalid upload size.')
                if not 0 < size <= 102 * 1024 * 1024:
                    raise HTTPException(413, 'The combined upload must be 102 MB or smaller.')
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        return await call_next(request)

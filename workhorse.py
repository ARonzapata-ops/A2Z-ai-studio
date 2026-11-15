"""A2Z's bounded, owner-only photo + audio video renderer (no AI API calls)."""
from contextlib import asynccontextmanager
import fcntl
import json
import logging
import os
import re
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
MAX_DURATION = 900
MAX_PHOTOS = 30
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


def lyric_cues(text, duration):
    """Require actual lyric timestamps; never invent timing from line count."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 200 or any(len(line) > 240 for line in lines):
        raise ValueError('Use at most 200 lyric lines, 240 characters per line.')
    pattern = re.compile(r'^\[(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\]\s*(.*)$')
    matches = [pattern.fullmatch(line) for line in lines]
    if any(matches):
        if not all(matches):
            raise ValueError('Use timestamps on every lyric line, or leave all lines untimed.')
        starts = []
        for match in matches:
            minutes, seconds, content = match.groups()
            if float(seconds) >= 60:
                raise ValueError('Lyric timestamp seconds must be below 60.')
            starts.append((int(minutes) * 60 + float(seconds), content))
        if any(t >= duration for t, _ in starts) or any(a[0] >= b[0] for a, b in zip(starts, starts[1:])):
            raise ValueError('Lyric timestamps must increase and fall within the video length.')
    elif lines:
        raise ValueError('Sync lyrics to your soundtrack first, or use timestamped lyrics. Plain text has no song timing.')
    else:
        return []
    return [(time, starts[i+1][0] if i+1 < len(starts) else duration, line) for i, (time, line) in enumerate(starts)]


def ass_time(seconds):
    value = round(max(0, seconds) * 100)
    return f'{value // 360000}:{value // 6000 % 60:02}:{value // 100 % 60:02}.{value % 100:02}'


def caption_file(path, cues, offset, seconds, width, height):
    # Text cannot introduce ASS styling, drawings, extra events or line breaks.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyrics,DejaVu Sans,{36 if height > width else 32},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,55,55,{int(height * .13)},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for start, end, text in cues:
        a, b = max(0, start-offset), min(seconds, end-offset)
        if b <= a:
            continue
        safe = text.replace('\\', '／').replace('{', '｛').replace('}', '｝')
        safe = ''.join(c for c in safe if ord(c) >= 32)
        events.append(f'Dialogue: 0,{ass_time(a)},{ass_time(b)},Lyrics,,0,0,0,,{safe}\n')
    path.write_text(header + ''.join(events), encoding='utf-8')


def scene_filter(width, height, frames, index, motion, fades):
    seconds = frames / 24
    filters = [f'scale={width}:{height}:force_original_aspect_ratio=decrease',
               f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2', 'setsar=1']
    if motion != 'none':
        denominator = max(1, frames-1)
        zoom = f'1.12-0.10*on/{denominator}' if index % 2 else f'1.02+0.10*on/{denominator}'
        x = 'iw/2-iw/zoom/2'
        y = 'ih/2-ih/zoom/2'
        if motion == 'pan':
            zoom = '1.12'
            x = f'(iw-iw/zoom)*on/{denominator}' if index % 2 else f'(iw-iw/zoom)*(1-on/{denominator})'
        filters.append(f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps=24")
    if fades:
        fade = min(.4, seconds/4)
        filters += [f'fade=t=in:st=0:d={fade}', f'fade=t=out:st={seconds-fade}:d={fade}']
    filters.append('format=yuv420p')
    return ','.join(filters)


def render(identifier):
    directory = ROOT / identifier
    spec = json.loads((directory / 'input.json').read_text())
    count = spec['count']
    width, height = (720, 1280) if spec['format'] == 'vertical' else (1280, 720)
    cues = lyric_cues(spec.get('lyrics', ''), spec['duration'])
    frames_total = spec['duration'] * 24
    offset = 0
    for i in range(count):
        frames = frames_total // count + (1 if i < frames_total % count else 0)
        seconds = frames / 24
        motion = spec.get('motion', 'none')
        filters = scene_filter(width, height, frames, i, motion, spec.get('fades', False))
        if cues:
            captions = directory / f'lyrics-{i}.ass'
            caption_file(captions, cues, offset, seconds, width, height)
            filters += f",subtitles=filename='{captions}'"
        loop = ['-loop', '1', '-framerate', '24'] if motion == 'none' else []
        command(['-threads', '1', *loop, '-i', str(directory / f'photo-{i}.jpg'),
                 '-frames:v', str(frames), '-vf', filters,
                 '-filter_threads', '1', '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '1', str(directory / f'scene-{i}.mp4')], directory)
        offset += seconds
        update(identifier, 'running', int((i + 1) / count * 85))
    playlist = directory / 'scenes.txt'
    playlist.write_text(''.join(f"file 'scene-{i}.mp4'\n" for i in range(count)))
    audio_filter = 'apad'
    if spec.get('fades', False):
        audio_filter += f",afade=t=in:st=0:d=0.3,afade=t=out:st={spec['duration']-.6}:d=0.6"
    update(identifier, 'running', 90, 'Adding your soundtrack and finishing the download…')
    command(['-f', 'concat', '-safe', '1', '-i', str(playlist), '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'wav,mp3,mov,ogg,flac,aac,matroska,webm', '-i', str(directory / 'audio'),
             '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
             '-af', audio_filter, '-t', str(spec['duration']), '-movflags', '+faststart', '-threads', '1', str(directory / 'video.mp4')], directory)
    if not (directory / 'video.mp4').stat().st_size:
        raise RuntimeError('The renderer returned an empty video.')
    update(identifier, 'complete', 100)
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
            'ai_video_generation': False, 'version': '2.1', 'max_duration_seconds': MAX_DURATION, 'max_photos': MAX_PHOTOS,
            'camera_motion': ['none', 'zoom', 'pan'], 'fade_transitions': True, 'lyric_captions': True, 'lyric_timestamps_required': True,
            'retention_hours': 24, 'storage': 'server-local; survives restarts only with a persistent disk'}


@router.post('/api/workhorse/jobs', dependencies=[Depends(owner)], status_code=202)
async def submit(photos: list[UploadFile] = File(...), audio: UploadFile = File(...), duration: int = Form(60), format: str = Form('vertical'),
                 motion: str = Form('zoom'), fades: bool = Form(True), lyrics: str = Form('', max_length=20000)):
    if not ffmpeg():
        raise HTTPException(503, 'FFmpeg is not installed on this server.')
    if not 1 <= len(photos) <= MAX_PHOTOS or not 5 <= duration <= MAX_DURATION or format not in {'vertical', 'landscape'}:
        raise HTTPException(422, 'Use 1–30 photos, 5–900 seconds, and vertical or landscape format.')
    if motion not in {'none', 'zoom', 'pan'}:
        raise HTTPException(422, 'Choose still, zoom or pan camera movement.')
    try:
        lyric_cues(lyrics, duration)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
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
        (directory / 'input.json').write_text(json.dumps({'count': len(photos), 'duration': duration, 'format': format, 'motion': motion, 'fades': fades, 'lyrics': lyrics}))
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
                if not 0 < size <= 202 * 1024 * 1024:
                    raise HTTPException(413, 'The combined upload must be 202 MB or smaller.')
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        return await call_next(request)

"""Opt-in provider gateway. Disabled until durable storage, tokens and limits exist."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from uuid import uuid4
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix='/api/ai')
MODELS = {'cover': ('fal', 'fal-ai/flux/schnell'), 'sfx': ('elevenlabs', 'eleven_text_to_sound_v2')}

def database():
    root = os.getenv('A2Z_AI_DATA_DIR', '')
    if not root or not Path(root).is_absolute() or not Path(root).is_dir():
        raise HTTPException(503, 'AI storage is not configured. Your local tools still work.')
    db = sqlite3.connect(str(Path(root) / 'jobs.sqlite'), timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, nonce TEXT, model TEXT, prompt TEXT, status TEXT, provider_id TEXT, result TEXT, cost INTEGER, day TEXT, created REAL, UNIQUE(owner,nonce))')
    return db

def authenticate(request):
    supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
    # Operator provisions scoped visitor tokens separately; no owner/provider key reuse.
    try:
        tokens = json.loads(os.getenv('A2Z_AI_VISITOR_TOKENS', '{}'))
    except ValueError:
        tokens = {}
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    for user, hashed in tokens.items():
        if supplied and isinstance(hashed, str) and secrets.compare_digest(digest, hashed):
            return user
    raise HTTPException(401, 'A valid visitor access token is required.')

def price(model):
    try:
        value = int(os.getenv('A2Z_AI_'+model.upper()+'_RESERVE_CENTS', '0'))
    except ValueError:
        value = 0
    return max(0, value)

def configured(model):
    return bool(os.getenv('FAL_KEY' if model == 'cover' else 'ELEVENLABS_API_KEY') and price(model))

def public(row):
    d = dict(row)
    d.pop('owner', None)
    d.pop('nonce', None)
    d['provider'], d['provider_model'] = MODELS[d['model']]
    d['result'] = json.loads(d['result']) if d['result'] else None
    return d

@router.get('/capabilities')
def capabilities(request: Request):
    authenticate(request)
    with database() as db:
        pass
    return {'models': [{'id': k, 'provider': v[0], 'model': v[1], 'configured': configured(k), 'reserve_cents': price(k), 'generation_verified': False} for k,v in MODELS.items()], 'billing': 'Conservative reservation, not final provider billing. Failed and uncertain requests retain their reservation.'}

class Creation(BaseModel):
    model: str
    prompt: str = Field(min_length=1, max_length=2000)
    nonce: str = Field(min_length=16, max_length=80)
    accepted_reserve_cents: int

@router.post('/jobs')
async def create_job(body: Creation, request: Request):
    owner = authenticate(request)
    if body.model not in MODELS or not configured(body.model):
        raise HTTPException(503, 'This model is not configured.')
    if not body.prompt.strip():
        raise HTTPException(422, 'Describe the asset first.')
    reserve = price(body.model)
    if body.accepted_reserve_cents != reserve:
        raise HTTPException(409, 'The cost reservation changed. Review the current quote.')
    try:
        daily = int(os.getenv('A2Z_AI_DAILY_CAP_CENTS', '0'))
        per_user = int(os.getenv('A2Z_AI_USER_DAILY_CAP_CENTS', '0'))
    except ValueError:
        raise HTTPException(503, 'Usage limits need configuration.')
    day = time.strftime('%Y-%m-%d', time.gmtime())
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM jobs WHERE owner=? AND nonce=?', (owner,body.nonce)).fetchone()
        if old:
            if old['model'] != body.model or old['prompt'] != body.prompt:
                raise HTTPException(409, 'This request ID belongs to different input.')
            return public(old)
        total = db.execute('SELECT COALESCE(SUM(cost),0) FROM jobs WHERE day=?', (day,)).fetchone()[0]
        used = db.execute('SELECT COALESCE(SUM(cost),0) FROM jobs WHERE day=? AND owner=?', (day,owner)).fetchone()[0]
        if daily <= 0 or per_user <= 0 or total+reserve > daily or used+reserve > per_user:
            raise HTTPException(429, 'The daily AI allowance is reached or disabled. Local tools remain available.')
        jid = str(uuid4())
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (jid,owner,body.nonce,body.model,body.prompt,'submitting',None,None,reserve,day,time.time()))
    # No automatic retries: a timeout may already have incurred a provider charge.
    status, provider_id, result = 'uncertain', None, None
    try:
        async with httpx.AsyncClient(timeout=100) as client:
            if body.model == 'cover':
                response = await client.post('https://queue.fal.run/fal-ai/flux/schnell', headers={'Authorization':'Key '+os.environ['FAL_KEY']}, json={'prompt':body.prompt,'num_images':1,'image_size':'square_hd','num_inference_steps':4})
                if response.is_success:
                    provider_id = response.json()['request_id']
                    status = 'queued'
            else:
                response = await client.post('https://api.elevenlabs.io/v1/sound-generation', headers={'xi-api-key':os.environ['ELEVENLABS_API_KEY']}, json={'text':body.prompt,'duration_seconds':5,'model_id':MODELS['sfx'][1]})
                if response.is_success and response.headers.get('content-type','').startswith('audio/') and 0 < len(response.content) <= 20*1024*1024:
                    Path(os.environ['A2Z_AI_DATA_DIR'], jid+'.mp3').write_bytes(response.content)
                    result = {'local_audio':True}
                    status = 'succeeded'
            if 400 <= response.status_code < 500:
                status = 'failed'
    except Exception:
        pass
    with database() as db:
        db.execute('UPDATE jobs SET status=?,provider_id=?,result=? WHERE id=?',(status,provider_id,json.dumps(result),jid))
        return public(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())

@router.get('/jobs')
def list_jobs(request: Request):
    owner = authenticate(request)
    with database() as db:
        return [public(r) for r in db.execute('SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 50',(owner,))]

@router.get('/jobs/{jid}')
async def get_job(jid: str, request: Request):
    owner = authenticate(request)
    with database() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(jid,owner)).fetchone()
    if not row:
        raise HTTPException(404, 'Job not found.')
    if row['status'] == 'queued' and row['provider_id']:
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                url = 'https://queue.fal.run/fal-ai/flux/requests/'+row['provider_id']
                headers = {'Authorization':'Key '+os.environ['FAL_KEY']}
                check = await client.get(url+'/status',headers=headers)
                if check.is_success and check.json().get('status') == 'COMPLETED':
                    response = await client.get(url,headers=headers)
                    if response.is_success:
                        output = response.json()
                        image_url = output['images'][0]['url']
                        from urllib.parse import urlparse
                        parsed = urlparse(image_url)
                        if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.fal.media'):
                            raise ValueError('Unexpected media host')
                        result = {'image_url':image_url}
                        with database() as db:
                            db.execute('UPDATE jobs SET status=?,result=? WHERE id=?',('succeeded',json.dumps(result),jid))
                            row = db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
                    elif 400 <= response.status_code < 500:
                        with database() as db:
                            db.execute('UPDATE jobs SET status=? WHERE id=?',('failed',jid))
                            row = db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        except Exception:
            pass  # Polling failures never resubmit generation.
    return public(row)

@router.get('/jobs/{jid}/audio')
def audio(jid: str, request: Request):
    owner = authenticate(request)
    with database() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(jid,owner)).fetchone()
    if not row or row['model'] != 'sfx' or row['status'] != 'succeeded':
        raise HTTPException(404, 'Audio not found.')
    return FileResponse(Path(os.environ['A2Z_AI_DATA_DIR'],row['id']+'.mp3'),media_type='audio/mpeg',filename='a2z-effect.mp3')

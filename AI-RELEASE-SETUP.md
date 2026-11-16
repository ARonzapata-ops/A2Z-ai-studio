# A2Z Release room

## Shipped in this update

- Release workspace with exact approved-line checks, style notes, artist, lyrics, caption, checklist and attached assets saved in IndexedDB.
- Finished WAV handoff from the existing song editor; song + cover handoff into the existing video renderer.
- Downloadable release ZIP and restorable release backup; editable multitrack mixes still use the audio editor backup.
- fal FLUX schnell cover generation and ElevenLabs five-second sound effects via server-only credentials.
- Scoped visitor-token authentication, durable SQLite history, request deduplication, global/per-user daily reservations, no automatic generation retries, output recovery.

## Required operator configuration before paid AI can work

On the existing backend, set:

- `FAL_KEY` and/or `ELEVENLABS_API_KEY`: provider API credentials. Never commit them or enter them into the public frontend.
- `A2Z_AI_DATA_DIR`: absolute path to an **existing persistent disk directory**, e.g. `/var/data/a2z-ai`. Provision storage before setting this. SQLite and effect files live here. Back up this directory. Use one server instance; shared multi-instance deployment needs a shared transactional database and object storage.
- `A2Z_AI_VISITOR_TOKENS`: JSON mapping visitor IDs to SHA256 hex digests of separately issued random visitor tokens. Raw visitor tokens are entered by each visitor only in the current page; provider and owner keys are never used here. Provision tokens through your normal secure admin process. There is no self-service account/billing system in this release.
- `A2Z_AI_COVER_RESERVE_CENTS`, `A2Z_AI_SFX_RESERVE_CENTS`: conservative per-request reservations, determined after checking current provider pricing. Zero/missing disables that model.
- `A2Z_AI_DAILY_CAP_CENTS`, `A2Z_AI_USER_DAILY_CAP_CENTS`: global and per-visitor daily reservation allowances. Zero/missing disables new requests. Reservations reset at UTC midnight. Failed/uncertain requests retain the reservation.

Reservations are admission controls based on operator estimates, not metered billing or a guarantee on dollar spend. Set provider-side spend limits too. Use separate visitor tokens to revoke one visitor without affecting others. Do not share the owner's existing art key with visitors. This gateway does not change the pre-existing singing/art routes or their controls.

Deploy `server.py` alongside `ai_gateway.py`; existing requirements already include FastAPI, Pydantic and httpx. After deploying, use Check available models in Release room. Configuration status is not proof that provider access, credits or live output work. Run a real paid example for each provider after configuration and check output/charge in the provider dashboard.

## Recovery and limits

fal jobs are queued and recovered by polling the existing provider request ID. Polling does not resubmit generation. Save generated image output locally promptly; remote URLs are not permanent. ElevenLabs sound-effect files are saved server-side on the persistent disk. A crash or timeout during submission can leave a submitting/uncertain job: inspect provider history manually before making another request. No automatic refund or retry is performed.

The UI intentionally exposes two narrow tested adapter contracts, not an arbitrary model proxy. Replicate, AI footage, transcription/forced alignment, stem separation, Eleven Music and hosted vocal cleanup remain future adapters, not advertised as connected. Existing local vocal processing remains experimental. Video lyric cues still require manual review.

The release room holds one current release on each browser. Download a backup before replacing it. No cloud project synchronization, public accounts, checkout, automatic distribution or rights certification is implemented. Reference photos and generated examples from the user's actual music were not available for a three-project showcase in this update.

## Validation

`python -m unittest test_ai_gateway -v` checks authentication, disabled storage, per-user allowances, uncertain submission reservation, idempotency, nonce conflicts, quote changes, job isolation and queued fal output recovery with mocked provider responses. These tests incur no paid generation. JS syntax and Python compile checks cover the new source. Real provider calls and physical iPhone recording require separate verification.

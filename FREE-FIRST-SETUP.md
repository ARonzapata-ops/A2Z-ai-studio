# A2Z free-first release

## Available without accounts or paid APIs

- Existing recording, beats, editing, mix processing, local vocal tools and video export remain on-device.
- `free-lab.html` adds a local creative assistant using CPU-compatible SmolLM2 135M through Transformers.js 3.8.1, plus Qwen 2.5 0.5B and 1.5B models through WebLLM 0.2.88. The selected model is downloaded only after explicit consent and Load. Web Workers keep inference off the UI thread. CPU generation uses single-threaded WebAssembly with 128 output tokens and shortened context; the model is a basic helper rather than premium-quality writing. Stop terminates the worker and releases the model; selected cache can be removed.
- Lyrics, scene plans, artwork prompts, captions and text feedback stream into an editable draft. These models do not hear audio or generate sung music.
- A clearly labelled guided outline works without WebGPU or a model download. It is a template, not AI output.
- Three Canvas cover designs, with optional user photo and title/artist typography, export as 1600px PNGs and hand off to Release room. These are graphic designs, not AI-generated images.
- Text drafts, cover settings and kept release drafts save in IndexedDB. Original photo selections are temporary; keep/export the finished cover. Release backups preserve kept drafts through the existing `release.json`.
- Paid model connections are collapsed as optional. Local tools never automatically fall back to a paid API.

## Model licenses and dependencies

SmolLM2 135M Instruct: https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct (Apache-2.0).
ONNX conversion: https://huggingface.co/onnx-community/SmolLM2-135M-Instruct-ONNX.
Transformers.js: https://github.com/huggingface/transformers.js (Apache-2.0).

WebLLM: https://github.com/mlc-ai/web-llm (Apache-2.0).
Qwen 2.5 0.5B Instruct: https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct (Apache-2.0).
Qwen 2.5 1.5B Instruct: https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct (Apache-2.0).
MLC compiled weights are downloaded from the package's prebuilt model registry; A2Z does not train these models or bundle their weights. Preserve upstream notices when redistributing copies. Adaptation changes the app and inference workflow, not the underlying compute requirements. Initial model/runtime downloads require network access; browser eviction and CDN availability mean offline loading is not guaranteed. Memory limits and WebGPU availability vary across phones and computers. Unsupported devices retain non-AI tools.

## Optional free cloud backend: prepared, not activated

`free-cloud-worker.mjs`, `free-cloud-schema.sql` and `free-cloud-wrangler.jsonc` provide a Cloudflare Workers AI + D1 text gateway. It can be deployed separately from the existing Python/Render app; it does not require a paid Render disk. It uses only Cloudflare's AI binding and D1, not fal, ElevenLabs or other paid-provider fallback code.

Activation requires signing into the owner's Cloudflare account. Do not create/upgrade to a paid plan. Confirm the account is on Workers Free before enabling. Cloudflare's platform free quota provides the hard billing boundary; this code cannot detect later account-plan changes. If the account is upgraded, disable the gateway or reassess billing before use. Application request caps alone are not a dollar-spend guarantee.

Using the official Wrangler CLI from the repository root, after account sign-in:

1. Create a D1 database named `a2z-free-records` with `wrangler d1 create a2z-free-records`. Copy the returned actual database ID into `free-cloud-wrangler.jsonc`; never invent it.
2. Apply `free-cloud-schema.sql` to that database using `wrangler d1 execute a2z-free-records --remote --file free-cloud-schema.sql --config free-cloud-wrangler.jsonc`.
3. Provision random per-visitor tokens through the existing secure admin process. Set the Worker secret `VISITOR_TOKEN_HASHES` to JSON mapping visitor IDs to SHA256 hex digests of those tokens (`wrangler secret put VISITOR_TOKEN_HASHES --config free-cloud-wrangler.jsonc`). Never commit raw tokens or ask users to enter provider keys into the public app.
4. Check the current Cloudflare free model catalog and model terms. The prepared adapter uses `@cf/qwen/qwen2.5-coder-32b-instruct`, with a fixed 384 output-token limit. It is a general instruction-capable code model; creative quality requires evaluation. There is no silent substitution if it becomes unavailable.
5. Set `FREE_PLAN_CONFIRMED` to `true` only after confirming Workers Free; deploy with `wrangler deploy --config free-cloud-wrangler.jsonc`. The default configuration keeps generation disabled.
6. Enter the actual deployed origin and a visitor token in Free creative lab, check connection, then explicitly consent before sending an idea to cloud generation.

The fixed global cap is 12 requests/day and the fixed per-visitor cap is 3/day (UTC). A single atomic SQLite INSERT enforces caps under concurrency. Failed/uncertain calls consume their slot. Request IDs deduplicate submissions and cannot be reused with different input. No automatic retries. D1 stores outputs, status, task and a fingerprint, scoped by visitor identity. Recover drafts shows the current visitor's outputs. Scheduled cleanup removes records older than 30 days. This is not project/media cloud sync.

Cloudflare platform quota errors stop generation; local tools remain available. No automatic billing upgrades, paid-model substitution, public account system or paid subscriptions are included. Cloud backend activation and real cloud generation are not verified until account access and bindings exist.

## Validation

`node --test test_free_cloud.mjs` uses Node SQLite to verify disabled defaults, auth/origin restrictions, idempotency, recovery isolation, nonce conflict, simultaneous request caps, failure reservation, input limits, and labelled non-AI fallbacks. Provider calls are mocked and incur no credits. `node --check` validates browser source. Live browser checks cover persistence, cover export, release handoff and available local-AI device behavior; physical iPhone compatibility remains a separate check.

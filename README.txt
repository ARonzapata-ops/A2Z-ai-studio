A2Z VOICE AI STUDIO — STARTER BUILD
===================================

WHAT WORKS
- Mobile-first web app interface for Safari.
- Record voice samples (when hosted on HTTPS and microphone permission is allowed).
- Upload audio files from Files.
- Store projects and voice samples locally in IndexedDB on this device/browser.
- Save, reopen, duplicate, and delete song projects.
- Export imported/recorded voice samples as mono 16-bit PCM WAV when Safari can decode the source.
- Export/import project text backups (audio files are not included in JSON backups).
- Optional integration point for an AI singing model server.

IMPORTANT LIMITATION
This starter does NOT contain a trained singing model and does not generate singing by itself.
For actual AI singing, you need a model/server that accepts the documented multipart fields and
returns generated audio. The app expects:
POST /generate (or your configured URL), multipart/form-data:
  lyrics          text
  style           text
  voice_sample    audio file
  reference_audio optional audio file
  model_label     text
Response can be an audio file body, or JSON {"audio_url":"https://..."}.
If JSON is returned, audio_url must be reachable from the browser.
Your server must configure CORS for your app origin. Keep private API keys on the server;
never put them into index.html or browser JavaScript.

FILES
- index.html: entire app UI and client logic
- manifest.webmanifest: Home Screen/PWA metadata
- sw.js: basic offline caching
- README.txt: this file

HOW TO HOST FROM IPHONE USING GITHUB PAGES (FREE)
1. Open https://github.com in Safari and sign in/create an account.
2. Create a NEW PUBLIC repository named a2z-voice-ai-studio. Public repository means the code is public.
   Do not place private voice recordings, API keys, or sensitive data in this repository.
3. In the repository, choose Add file > Create new file. Name it index.html.
   Paste all contents of the provided index.html and commit the file.
4. Repeat Add file > Create new file for manifest.webmanifest and sw.js, pasting each provided file.
5. Open repository Settings > Pages.
6. Under Build and deployment, choose Deploy from a branch; select branch main and folder /(root), then Save.
7. Wait for the Pages URL to appear. Open it in Safari over HTTPS.
8. Test recording, save a project, then use Share > Add to Home Screen.
9. Export backups regularly. Local IndexedDB data can be erased if Safari website data is cleared.
   The app is served publicly, but recordings remain in this browser unless you explicitly send them
   to a configured AI endpoint.

LOCAL PREVIEW
Opening index.html directly from Files may block microphone access and service worker features.
For best results, host over HTTPS as described above.

SECURITY / PRIVACY
- GitHub Pages is static hosting; it cannot run the AI model backend.
- The site code is public. Never commit voice recordings or secret tokens to the repository.
- A model endpoint receives your lyrics and audio when you tap Generate. Use only a server you trust.
- The starter stores projects and voice audio in browser IndexedDB on this device. It does not upload
  them unless you initiate the Generate request.
- Voice model use should be limited to your own voice or voices you have permission to use.

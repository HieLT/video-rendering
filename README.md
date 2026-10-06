# Video Rendering

A high-performance session coordinator and OpenAI-compatible video generation API service.

Provides automated browser session isolation, task queue distribution, extended duration handling, and an intuitive web management dashboard.

---

## 🌟 Key Capabilities

1. **OpenAI-Compatible Video API**:
   - `POST /v1/videos/generations`: Submit generation tasks with prompt, aspect ratio, duration (`10s`, `15s`, `30s`), and reference images.
   - `GET /v1/videos/<id>`: Poll task lifecycle (`queued` -> `processing` -> `completed` / `failed`).
   - High-speed MP4 streaming and static asset delivery.
2. **Extended Duration & High-Definition Media Export**:
   - Integrated browser automation profile for managing extended duration options.
   - Direct original quality stream extraction and processing.
3. **Multi-Account Browser Pool**:
   - Manages multiple persistent browser profiles in `accounts/`.
   - Automatic concurrency management, mutual exclusion, and session rotation.
   - Built-in verification handling.
4. **Admin Web Dashboard**:
   - Real-time dashboard at `/web` to monitor generation trends, success rate, account statuses, task queues, and API key management.

---

## 📁 Repository Structure

```
video-rendering/
├── server.py              # FastAPI server (OpenAI-compatible video API & admin routes)
├── browser_pool.py        # Account pool concurrency manager and task scheduler
├── browser.py             # Playwright persistent context launcher
├── video_worker_ui.py     # UI automation worker with verification handler
├── video_worker.py        # Protocol worker and status polling
├── store.py               # SQLite task persistence and API key storage
├── dola_client.py         # API client communication module
├── media.py               # Reference media processor
├── config.py              # Configuration & environment variables
├── add_account.py         # Automated account profile setup
├── web/
│   └── index.html         # Single-page admin management dashboard
└── extensions/
    └── dola30/            # Chromium extension profile
```

---

## 🚀 Quick Start

### 1. Requirements
* Python 3.11+
* Chrome / Chromium browser
* Proxy with JP/KR egress

### 2. Setup Environment
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure
```bash
# Set your proxy configuration
export DOLA_PROXY="http://127.0.0.1:7890"

# Set API key for client authentication (optional, empty = dev mode)
export DOLA_API_KEYS="sk-your-secret-key"

# Concurrency limits
export DOLA_MAX_CONCURRENCY=5
```

### 4. Start Server
```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```
Open **http://127.0.0.1:8000/web** to access the Admin Dashboard.


### 🌐 SonicVoice (For Voice Clone)

[![Website](https://img.shields.io/badge/Website-SonicVoice.pro-6366f1?style=for-the-badge&logo=google-chrome&logoColor=white)](https://sonicvoice.pro)

### 💬 Admin & Support

[![Zalo](https://img.shields.io/badge/Zalo-Nhóm%20Zalo-0068FF?style=for-the-badge&logoColor=white)](https://zalo.me/g/jvwa05y9id3apkgfocw0)

---

## 📜 License
For educational and internal testing purposes.

### Renew Dola account (Google)

Accounts with Used Today 2/2 and type Google expose `Renew Dola account`. It deletes the remote Dola account and then signs in again using the same persistent Google browser profile. No password is stored. Complete password, verification, consent, or onboarding prompts directly in the browser (10-minute timeout). Facebook is not supported by this action.

The profile is locked during the job. A successful re-login is checked against Dola before dispatch resumes. Failed jobs remain unavailable for dispatch. A confirmed deletion can be continued using `Resume renewal`, including after a server restart; an uncertain deletion is blocked from automatic retry. After verified re-login, local usage resets to 0/2 and stale quota, cooldown, and credit state are cleared. Failed or unverified logins do not reset usage. The ordinary Delete button still removes the local profile.


### Browser queue and persisted video monitoring

All Python Playwright entry points use browser_queue.async_playwright.
The process-shared FIFO queue admits at most 10 driver sessions. Each retry,
scheduled check and manual browser operation takes a new ticket at the tail.
Run one uvicorn worker per workspace; a scheduler lock rejects a second server.

Video jobs persist their account reservation, ordered reference-image copies,
dispatch checkpoint, generation retry count and next check in SQLite.
After Dola confirms generation, Chromium closes. The first check uses the
ETA from the original API text minus five minutes (minimum zero wait).
If ETA is unavailable, the default 30-minute ETA means a 25-minute wait.
Each inspection polls for up to 20 seconds after page navigation. If pending,
there are at most three additional inspections, five minutes apart.
Jobs still unresolved move to the dashboard review filter (needs_recovery).

Review jobs keep their account reservation and count toward the 100 unfinished
job admission limit. Manual Check/Continue starts a fresh inspection cycle at
the FIFO tail without resetting the generation retry count. Stop monitoring
releases the account but does not cancel generation remotely or refund quota.
Open chat also goes through the shared queue.

There are at most 10 generation retries after the initial attempt. Transport
failures during an uncertain dispatch are reconciled against the saved
conversation; without sufficient evidence the job moves to review rather
than submitting a duplicate. Recovery checks never charge generation quota.
Generated videos with a download failure can resume their saved download.

Task-owned image copies live in .job_media/; keep this directory with
tasks.db for restart recovery. Runtime browser tickets live in .runtime/;
stale ticket owners are reclaimed using OS file locks.

Offline checks:
- python -B test_video_schedule.py
- python -B test_scheduler_integration.py
- python -B test_scheduler_dashboard.py
- python -B test_video_confirmation.py
- python -B test_resume_usage.py

### Start / End with environment references

In Start / End mode, choose the opening image and optionally an ending image, then add
Environment / extra references. The upload order is Start, End, then environment
images. The configured `DOLA_REFERENCE_IMAGE_MAX_COUNT` limit includes all images.
For API requests, set `start_end: true` and provide at least two images in
`reference_images` in this order when using an End image. For Start only, provide at least
one image and set `has_end_frame: false`; all images after Start are environment
references. With no explicit flag, one image means Start only, while two or more
retain the existing Start + End behavior. Additional images guide the environment,
lighting and scene details through the prompt; exact endpoint frames are not guaranteed.

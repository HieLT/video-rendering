"""Dola Pool Configuration: Environment variable based with smart defaults."""
import os
from pathlib import Path


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins."""
    path = Path(__file__).with_name(".env.local")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()

# Keep temporary media and child browser files on the project drive.
TEMP_DIR = Path(__file__).resolve().parent / ".runtime" / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
for _temp_key in ("TEMP", "TMP", "TMPDIR"):
    os.environ[_temp_key] = str(TEMP_DIR)
import tempfile
# tempfile may already have cached the Windows default before config loads.
tempfile.tempdir = str(TEMP_DIR)

HOST = os.getenv("DOLA_HOST", "0.0.0.0")
PORT = int(os.getenv("DOLA_PORT", "8000"))

# Service API keys (comma-separated; empty = no auth, local debug only)
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# Account pool cookie file (one dola.com cookie per line)
COOKIES_FILE = os.getenv("DOLA_COOKIES_FILE", "cookies.txt")

# Max concurrent video generation tasks
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "100"))
IMAGE_MAX_CONCURRENCY = max(1, int(os.getenv("DOLA_IMAGE_MAX_CONCURRENCY", "20")))
# Enable a headed, extension-backed image worker for controlled parity tests.
IMAGE_INTERACTIVE_WORKER = os.getenv("DOLA_IMAGE_INTERACTIVE_WORKER", "1") == "1"

# Controlled account-environment isolation.  When enabled, a browser account
# must have an explicitly assigned proxy, records its observed public egress,
# and refuses to visit Dola immediately after that egress changes.
STRICT_ACCOUNT_ENVIRONMENT = os.getenv("DOLA_STRICT_ACCOUNT_ENVIRONMENT", "0") == "1"
MIN_SUBMIT_INTERVAL_SECONDS = max(0, int(os.getenv("DOLA_MIN_SUBMIT_INTERVAL_SECONDS", "15")))
EGRESS_CHANGE_COOLDOWN_SECONDS = max(
    0, int(os.getenv("DOLA_EGRESS_CHANGE_COOLDOWN_SECONDS", "1800")))
EGRESS_PROBE_URL = os.getenv("DOLA_EGRESS_PROBE_URL", "https://api.ipify.org?format=json").strip()

# Global pending task queue limit (queued + processing), 0 = unlimited
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "500"))

# Video generation timeout in seconds
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "300"))

# SQLite database path
DB_PATH = os.getenv("DOLA_DB_PATH", "tasks.db")

# Video download storage directory (served statically by FastAPI)
DOWNLOAD_DIR = os.getenv("DOLA_DOWNLOAD_DIR", "downloads")

# Use per-account proxy routing; legacy environment proxy settings stay disabled.
PROXY_ENABLED = True
PROXY = ""
PROXY_USERNAME = ""
PROXY_PASSWORD = ""


def browser_proxy():
    return None


def proxy_auth():
    return None


# Run browser in headless mode (login always runs with head)
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"
# Optional system Chrome executable for environments where bundled Chromium is blocked.
BROWSER_EXECUTABLE = os.getenv("DOLA_BROWSER_EXECUTABLE", "").strip()

# Base public URL for returning static video links
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# Admin web dashboard password (empty = no auth)
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")

# Dola 30s / Watermark Removal Chromium extension path
EXTENSION_DIR = os.getenv("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Daily quota reset timezone (Japan midnight by default)
LIMIT_RESET_TZ = os.getenv("DOLA_LIMIT_RESET_TZ", "Asia/Tokyo")

# Conservative credit check before video generation (default 2 points)
VIDEO_REQUIRED_POINTS = int(os.getenv("DOLA_VIDEO_REQUIRED_POINTS", "2"))

# Public reference image download limits
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))

# Extended generation window for reference image tasks (seconds)
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "900"))

# Persistent reusable scene images; separate from temporary references and video outputs.
ASSET_DIR = os.getenv("DOLA_ASSET_DIR", "assets")

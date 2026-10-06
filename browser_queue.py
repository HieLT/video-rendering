"""Process-shared FIFO admission for every managed Playwright session."""
import asyncio
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from patchright.async_api import async_playwright as _playwright

ROOT = Path(__file__).resolve().parent / ".runtime"
LIMIT = 10

def lock_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    if f.tell() == 0:
        f.write(b"0"); f.flush()
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        f.close()
        return None
    return f

def unlock_file(f):
    if not f: return
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_UN)
    f.close()

class BrowserQueue:
    def __init__(self, root=ROOT, limit=LIMIT):
        self.root, self.limit = Path(root), min(LIMIT, max(1, limit))

    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / "browser_queue.db", timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT UNIQUE, active INTEGER DEFAULT 0)")
        return db

    @asynccontextmanager
    async def slot(self):
        owner = uuid.uuid4().hex
        path = self.root / (owner + ".lock")
        guard = lock_file(path)
        if guard is None: raise RuntimeError("Cannot acquire browser ticket")
        db = self.connect()
        try:
            with db:
                db.execute("INSERT INTO tickets(owner) VALUES (?)", (owner,))
            while True:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    # OS locks survive neither a process crash nor a machine restart.
                    for old, in db.execute("SELECT owner FROM tickets WHERE owner<>?", (owner,)).fetchall():
                        stale_path = self.root / (old + ".lock")
                        probe = lock_file(stale_path)
                        if probe is not None:
                            db.execute("DELETE FROM tickets WHERE owner=?", (old,))
                            unlock_file(probe)
                            stale_path.unlink(missing_ok=True)
                    first = db.execute("SELECT owner FROM tickets WHERE active=0 ORDER BY id LIMIT 1").fetchone()
                    active = db.execute("SELECT COUNT(*) FROM tickets WHERE active=1").fetchone()[0]
                    ready = first and first[0] == owner and active < self.limit
                    if ready:
                        db.execute("UPDATE tickets SET active=1 WHERE owner=?", (owner,))
                if ready: break
                await asyncio.sleep(.2)
            yield
        finally:
            with db: db.execute("DELETE FROM tickets WHERE owner=?", (owner,))
            db.close()
            unlock_file(guard)
            path.unlink(missing_ok=True)

QUEUE = BrowserQueue()

@asynccontextmanager
async def async_playwright():
    # No Node driver or Chromium is started while waiting for the FIFO ticket.
    async with QUEUE.slot():
        async with _playwright() as p:
            yield p

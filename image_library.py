"""Persistent, content-deduplicated reference image library."""
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import time
from io import BytesIO
from pathlib import Path

from PIL import Image


class ImageLibrary:
    def __init__(self, root):
        self.root = Path(root)

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / "library.db")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS images (id TEXT PRIMARY KEY, name TEXT NOT NULL, suffix TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL)")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, data, name, max_bytes):
        if len(data) > max_bytes:
            raise ValueError("Reference image exceeds single file size limit")
        with Image.open(BytesIO(data)) as image:
            image.verify()
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
        if not suffix:
            raise ValueError("Reference image only supports JPEG, PNG, WEBP")
        identity = hashlib.sha256(data).hexdigest()
        name = (name or "image").replace("\\", "/").split("/")[-1][:255]
        with self.connect() as db:
            path = self.root / (identity + suffix)
            if not path.exists():
                path.write_bytes(data)
            db.execute("INSERT OR IGNORE INTO images VALUES (?,?,?,?,?)", (identity, name, suffix, len(data), time.time()))
            return dict(db.execute("SELECT * FROM images WHERE id=?", (identity,)).fetchone())

    def list(self, offset=0, limit=60, search=""):
        with self.connect() as db:
            where = " WHERE instr(lower(name), lower(?)) > 0"
            total = db.execute("SELECT count(*) FROM images" + where, (search,)).fetchone()[0]
            rows = db.execute("SELECT * FROM images" + where + " ORDER BY created_at DESC, id LIMIT ? OFFSET ?", (search, limit, offset)).fetchall()
            return {"images": [dict(row) for row in rows], "total": total}

    def path(self, identity):
        if not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("Image not found")
        with self.connect() as db:
            row = db.execute("SELECT * FROM images WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Image not found")
        path = self.root / (identity + row["suffix"])
        if not path.is_file():
            raise ValueError("Image not found")
        return path


    def import_path(self, path):
        path = Path(path)
        data = path.read_bytes()
        return self.save(data, path.name, len(data))

    def usage(self, task_db, identity):
        """Include completed jobs: they may still need their original inputs."""
        path = self.path(identity).resolve()
        count = 0
        for row in task_db.execute("SELECT reference_images, reference_paths FROM tasks WHERE deleted_at IS NULL"):
            sources, paths = (json.loads(value or "[]") for value in row)
            if ("library://" + identity in sources or
                any(Path(p).resolve() == path for p in paths) or
                any(s.startswith("local://") and Path(s[8:]).resolve() == path for s in sources)):
                count += 1
        return count

    def delete(self, identity):
        path = self.path(identity)
        with self.connect() as db:
            path.unlink()
            db.execute("DELETE FROM images WHERE id=?", (identity,))

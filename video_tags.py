"""Local edit selections: keep originals and maintain one copy per task."""
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit


def _child(root, filename):
    root = Path(root).resolve()
    if not filename or Path(filename).name != filename or '/' in filename or '\\' in filename:
        raise ValueError('Invalid video filename')
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('Video path must stay inside its directory')
    return path


def set_edit_selection(store, row, selected, download_dir, tag_dir):
    """Caller holds the task lock; DB failures restore the previous copy state."""
    root = Path(tag_dir).resolve()
    filename = row.get('tag_filename')
    if not selected:
        target = _child(root, filename) if filename else None
        backup = None
        if target and target.exists():
            fd, temporary = tempfile.mkstemp(prefix='.untag-', dir=root)
            os.close(fd)
            backup = Path(temporary)
            target.replace(backup)
        try:
            store.update(row['id'], edit_selected=0, tag_filename=None)
        except Exception:
            if backup:
                backup.replace(target)
            raise
        if backup:
            backup.unlink()
        return None

    if row['status'] != 'completed' or not row.get('video_url'):
        raise ValueError('Only completed videos can be selected for editing')
    url_path = unquote(urlsplit(row['video_url']).path)
    if not url_path.startswith('/videos/'):
        raise ValueError('Video is not a local download')
    source = _child(download_dir, url_path[len('/videos/'):])
    if not source.is_file():
        raise FileNotFoundError('Original video is missing from downloads')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', row['id']):
        raise ValueError('Invalid task ID')
    if not filename:
        scene = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', row.get('name') or 'video').strip(' .')[:80] or 'video'
        filename = f"{scene}__{row['id']}.mp4"
    root.mkdir(parents=True, exist_ok=True)
    target = _child(root, filename)
    if target == source:
        raise ValueError('Tag copy must be separate from the original')
    if row.get('edit_selected') and target.is_file():
        return filename
    if target.exists():
        raise ValueError('A different file already exists at the tag destination')
    fd, temporary = tempfile.mkstemp(prefix='.tag-', dir=root)
    os.close(fd)
    temporary = Path(temporary)
    installed = False
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
        installed = True
        store.update(row['id'], edit_selected=1, tag_filename=filename)
    except Exception:
        if installed:
            target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return filename

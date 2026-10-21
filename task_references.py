"""Read-only, authenticated previews of a task's ordered input images."""
import json
from pathlib import Path

from fastapi import Header, HTTPException
from fastapi.responses import FileResponse
from asset_storage import asset_root, resolve_asset_path


def _values(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, list) else []
    except ValueError:
        return []


def reference_files(row, library, project_root):
    saved = _values(row.get('reference_paths'))
    sources = _values(row.get('reference_images'))
    snapshot = _values(row.get('reference_snapshot'))
    result = []
    roots = [library.root.resolve(), (project_root / '.job_media').resolve(), asset_root()]
    for index in range(max(len(saved), len(sources), len(snapshot))):
        path = None
        try:
            if index < len(saved):
                path = Path(saved[index]).resolve()
            elif index < len(snapshot):
                path = resolve_asset_path(snapshot[index]['file_path'])
            elif sources[index].startswith('library://'):
                path = library.path(sources[index][10:]).resolve()
            if path and (path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}
                         or not any(path.is_relative_to(root) for root in roots)
                         or not path.is_file()):
                path = None
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            path = None
        result.append(path)
    return result


def register_task_reference_routes(app, store, auth, library, project_root):
    def files(task_id):
        row = store.get(task_id)
        if not row or row.get('deleted_at'):
            raise HTTPException(404, 'Task not found')
        return reference_files(row, library, project_root)

    @app.get('/api/admin/tasks/{task_id}/references')
    async def references(task_id: str, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        return {'images': [dict(index=i + 1, available=path is not None,
                                url=f'/api/admin/tasks/{task_id}/references/{i + 1}')
                           for i, path in enumerate(files(task_id))]}

    @app.get('/api/admin/tasks/{task_id}/references/{index}')
    async def reference_image(task_id: str, index: int,
                              x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        paths = files(task_id)
        if index < 1 or index > len(paths) or paths[index - 1] is None:
            raise HTTPException(404, 'Saved input image is unavailable')
        return FileResponse(paths[index - 1], headers={'Cache-Control': 'private, no-store'})

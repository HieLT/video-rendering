"""Adapt persistent scene inputs to the existing task/worker pipeline."""
import json

from asset_storage import resolve_asset_path


def build_reference_snapshot(references):
    return [{key: row[key] for key in ("asset_id", "reference_alias", "position", "file_path")}
            for row in references]


def task_reference_paths(task):
    raw = task.get("reference_snapshot")
    references = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(references, list):
        raise ValueError("Invalid persistent reference snapshot")
    paths = []
    for ref in references:
        path = resolve_asset_path(ref["file_path"])
        if not path.is_file():
            raise FileNotFoundError(f"Persistent source image is missing: {ref['asset_id']}")
        paths.append(str(path))
    return paths


def prepare_scene_request(scene, references, request_factory, count=1):
    """One scene preparation path for regenerate and project orchestration."""
    snapshot = build_reference_snapshot(references)
    name = (f"scene{scene['scene_number']}_{scene['project_name']}"[:200]
            if scene.get('project_name') else scene['scene_name'])
    req = request_factory(name=name, prompt=scene['prompt'], model=scene['model'],
                          ratio=scene['ratio'], duration=scene['duration'], start_end=bool(scene['start_end']),
                          reference_aliases=[r['reference_alias'] for r in references], count=count)
    return req, snapshot

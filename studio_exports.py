"""Materialized, human-readable exports for Film Studio artifacts."""
import json
import re
import shutil
from pathlib import Path


def sync_reference_export(flow, image_library, run_id, export_root):
    """Copy the run's current reference set into one stable named folder."""
    run = flow.get(run_id)
    folder = Path(export_root).resolve() / run_id / 'references'
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for reference_id, artifact_id in run.get('images', {}).items():
        artifact = next(a for a in run['artifacts'] if a['id'] == artifact_id)
        image_id = artifact['content']['image_id']
        source = Path(image_library.path(image_id)).resolve()
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', reference_id).strip('._') or image_id[:12]
        target = folder / (safe_name + source.suffix.lower())
        temp = folder / (target.name + '.tmp')
        shutil.copy2(source, temp)
        temp.replace(target)
        # A regenerated reference may change file format. Keep only the
        # current extension for this exact sanitized reference name.
        for stale in folder.glob(safe_name + '.*'):
            if stale != target and stale.name != target.name + '.tmp' and stale.is_file():
                stale.unlink()
        rows.append({'reference_id': reference_id, 'artifact_id': artifact_id,
                     'image_id': image_id, 'file': target.name})
    manifest = {'run_id': run_id, 'title': run['title'], 'references': rows}
    pending = folder / 'manifest.json.tmp'
    pending.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    pending.replace(folder / 'manifest.json')
    return folder

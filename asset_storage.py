"""Safe persistent asset paths. These helpers never create or delete files."""
from pathlib import Path
import re

import config

SUPPORTED_ASSET_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PROJECT_ROOT = Path(__file__).resolve().parent


def asset_root():
    root = Path(config.ASSET_DIR).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve()


def asset_relative_path(project_id, asset_id, extension):
    for value in (project_id, asset_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Asset project and asset IDs must be safe path components")
    if not isinstance(extension, str) or extension.lower() not in SUPPORTED_ASSET_EXTENSIONS:
        raise ValueError("Asset extension must be .jpg, .jpeg, .png, or .webp")
    return f"{project_id}/{asset_id}{extension.lower()}"


def resolve_asset_path(file_path):
    if not isinstance(file_path, str) or not file_path or "\\" in file_path:
        raise ValueError("Asset path must be a relative POSIX path")
    relative = Path(file_path)
    if relative.is_absolute() or any(part in (".", "..") for part in file_path.split("/")) or ":" in file_path:
        raise ValueError("Asset path must stay inside asset storage")
    root = asset_root()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("Asset path escapes asset storage")
    return target


def validate_asset_path(project_id, asset_id, file_path):
    extension = Path(file_path).suffix
    expected = asset_relative_path(project_id, asset_id, extension)
    if file_path != expected:
        raise ValueError("Asset path must be <project_id>/<asset_id>.<supported extension>")
    resolve_asset_path(file_path)
    return expected

"""Template 5 import contract and deterministic reference-name matching."""
from collections import defaultdict
import json
import sqlite3

import config
from reference_aliases import resolve_reference_aliases

SUPPORTED_MODEL_KEYS = {
    "seedance_2.0", "seedance_2.5", "seedance_v2.0", "seedance_v2.5",
    "seedance_20", "seedance_25", "seedance_v20", "seedance_v25",
}
SUPPORTED_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}
SCENE_FIELDS = {"scene_number", "scene_name", "prompt", "model", "ratio", "duration", "start_end"}


class AssetNameCollisionError(sqlite3.IntegrityError):
    def __init__(self, collisions):
        self.collisions = collisions
        super().__init__("Ambiguous asset names; no assets were renamed: " + json.dumps(collisions, ensure_ascii=False))


class ImportValidationError(ValueError):
    def __init__(self, report):
        self.report = report
        self.status_code = 409 if report['conflicts'] and not report['validation_errors'] else 422
        super().__init__('Scene import validation failed; no scenes were written')


class SceneNotReadyError(ValueError):
    def __init__(self, details):
        self.details = details
        self.status_code = 409 if details['missing_references'] else 422
        super().__init__('Scene is not ready for generation')


def asset_name_key(name):
    return name.strip().casefold()


def normalize_reference_alias(raw):
    if not isinstance(raw, str):
        raise ValueError('reference_alias must be a string')
    alias = raw.strip().removeprefix('@').strip()
    if not alias or '@' in alias:
        raise ValueError('reference_alias must be non-empty and contain no @')
    return alias


def validate_aliases(aliases):
    if len({alias.casefold() for alias in aliases}) != len(aliases):
        raise ValueError('Duplicate reference alias (case-insensitive)')
    resolve_reference_aliases('', aliases, len(aliases))


def find_asset_name_collisions(assets):
    grouped = defaultdict(list)
    for asset in assets:
        grouped[(asset['project_id'], asset_name_key(asset['name']))].append({'id':asset['id'], 'name':asset['name']})
    return [{'project_id':project, 'name_key':key, 'assets':rows}
            for (project, key), rows in sorted(grouped.items()) if len(rows) > 1]


def asset_name_index(assets):
    collisions = find_asset_name_collisions(assets)
    if collisions:
        raise AssetNameCollisionError(collisions)
    return {asset_name_key(asset['name']):asset for asset in assets}


def configuration_errors(scene, references):
    errors = []
    if not isinstance(scene.get('prompt'), str) or not scene['prompt'].strip():
        errors.append('prompt must not be empty before generation')
    model = scene.get('model')
    if not isinstance(model, str) or model.lower().replace('-', '_') not in SUPPORTED_MODEL_KEYS:
        errors.append('unsupported model')
    if scene.get('ratio') not in SUPPORTED_RATIOS:
        errors.append('unsupported ratio')
    if type(scene.get('duration')) is not int or scene['duration'] not in (10,15,30):
        errors.append('duration must be 10, 15, or 30')
    if type(scene.get('start_end')) not in (bool, int) or scene['start_end'] not in (0,1):
        errors.append('start_end must be boolean')
    if scene.get('start_end') and len(references) != 2:
        errors.append('Start / End requires exactly two references in start/end order')
    if len(references) > config.REFERENCE_IMAGE_MAX_COUNT:
        errors.append('too many reference images')
    try:
        validate_aliases([ref['alias'] for ref in references])
        if isinstance(scene.get('prompt'), str):
            resolve_reference_aliases(scene['prompt'], [ref['alias'] for ref in references], len(references))
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def validate_import_payload(payload, assets, existing_scenes):
    """Return a complete normalized plan/report without mutating input or storage."""
    report = dict(valid=False, scene_count=0, unique_reference_count=0,
                  matched_reference_count=0, missing_reference_count=0,
                  references_matched=[], references_missing=[], conflicts=[], validation_errors=[], scenes=[])
    def error(path, message, code='invalid_field'):
        report['validation_errors'].append({'path':path, 'code':code, 'message':message})
    if not isinstance(payload, dict) or set(payload) != {'scenes'}:
        error('$', 'Payload must be an object containing only scenes')
        return report
    raw_scenes = payload['scenes']
    if not isinstance(raw_scenes, list) or not raw_scenes:
        error('scenes', 'scenes must be a non-empty array')
        return report
    report['scene_count'] = len(raw_scenes)
    try:
        lookup = asset_name_index(assets)
    except AssetNameCollisionError as exc:
        error('references', str(exc), 'ambiguous_asset_names')
        return report
    existing_numbers = {row['scene_number']:row['id'] for row in existing_scenes}
    seen_numbers = set()
    unique_references = {}
    for index, raw in enumerate(raw_scenes):
        path = f'scenes[{index}]'
        if not isinstance(raw, dict):
            error(path, 'Scene must be an object')
            continue
        required = SCENE_FIELDS | {'references'}
        for field in sorted(required - set(raw)):
            error(path + '.' + field, 'Required field is missing')
        for field in sorted(set(raw) - required):
            error(path + '.' + field, 'Unknown scene field')
        number = raw.get('scene_number')
        if type(number) is not int or number < 1:
            error(path + '.scene_number', 'scene_number must be an integer >= 1')
        else:
            if number in seen_numbers:
                error(path + '.scene_number', 'Duplicate scene_number in payload', 'duplicate_scene_number')
            seen_numbers.add(number)
            if number in existing_numbers:
                report['conflicts'].append({'scene_number':number, 'scene_id':existing_numbers[number]})
        for field in ('scene_name','prompt'):
            if not isinstance(raw.get(field), str):
                error(path + '.' + field, field + ' must be a string')
        model = raw.get('model')
        if not isinstance(model, str) or model.lower().replace('-', '_') not in SUPPORTED_MODEL_KEYS:
            error(path + '.model', 'Supported models are seedance-2.0 and seedance-2.5 (including current API aliases)')
        if not isinstance(raw.get('ratio'), str) or raw['ratio'] not in SUPPORTED_RATIOS:
            error(path + '.ratio', 'Supported ratios: ' + ', '.join(sorted(SUPPORTED_RATIOS)))
        if type(raw.get('duration')) is not int or raw['duration'] not in (10,15,30):
            error(path + '.duration', 'duration must be integer 10, 15, or 30')
        if type(raw.get('start_end')) is not bool:
            error(path + '.start_end', 'start_end must be a JSON boolean')
        references = raw.get('references')
        if not isinstance(references, list):
            error(path + '.references', 'references must be an array')
            references = []
        if len(references) > config.REFERENCE_IMAGE_MAX_COUNT:
            error(path + '.references', 'Too many reference images')
        if raw.get('start_end') is True and len(references) != 2:
            error(path + '.references', 'Start / End requires exactly two references in start/end order')
        normalized = []
        seen_names = set()
        seen_assets = set()
        aliases = []
        for position, reference in enumerate(references, 1):
            ref_path = f'{path}.references[{position-1}]'
            if not isinstance(reference, dict) or set(reference) != {'name','alias'}:
                error(ref_path, 'Reference must contain exactly name and alias')
                continue
            name = reference['name']
            if not isinstance(name, str) or not name.strip():
                error(ref_path + '.name', 'Reference name must be a non-empty string')
                continue
            name = name.strip()
            try:
                alias = normalize_reference_alias(reference['alias'])
            except ValueError as exc:
                error(ref_path + '.alias', str(exc))
                continue
            key = asset_name_key(name)
            asset = lookup.get(key)
            if key in seen_names or (asset and asset['id'] in seen_assets):
                error(ref_path + '.name', 'Same reference/asset occurs twice in this scene', 'duplicate_reference')
            if alias.casefold() in {a.casefold() for a in aliases}:
                error(ref_path + '.alias', 'Duplicate reference alias (case-insensitive)', 'duplicate_alias')
            seen_names.add(key)
            if asset:
                seen_assets.add(asset['id'])
            aliases.append(alias)
            normalized.append({'name':name, 'alias':alias, 'position':position, 'asset_id':asset['id'] if asset else None})
            unique_references.setdefault(key, {'name':name, 'asset_id':asset['id'] if asset else None})
        # Resolve against all declared aliases, including missing assets, only for
        # validation. The original prompt is never replaced by this resolved text.
        if isinstance(raw.get('prompt'), str) and aliases:
            try:
                resolve_reference_aliases(raw['prompt'], aliases, len(aliases))
            except ValueError as exc:
                error(path + '.prompt', str(exc), 'unknown_reference_alias')
        scene = {key:raw.get(key) for key in SCENE_FIELDS}
        scene['references'] = normalized
        report['scenes'].append(scene)
    report['unique_reference_count'] = len(unique_references)
    report['references_matched'] = [row for row in unique_references.values() if row['asset_id'] is not None]
    report['references_missing'] = [{'name':row['name']} for row in unique_references.values() if row['asset_id'] is None]
    report['matched_reference_count'] = len(report['references_matched'])
    report['missing_reference_count'] = len(report['references_missing'])
    report['valid'] = not report['validation_errors'] and not report['conflicts']
    return report

"""Admin APIs for persistent references and the project production workflow."""
import asyncio
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from asset_storage import validate_asset_path, resolve_asset_path

import config
from asset_library import AssetLibrary
from production_store import AssetInUseError, RecordNotFoundError
from scene_generation import build_reference_snapshot, prepare_scene_request
from scene_import import ImportValidationError, SceneNotReadyError, AssetNameCollisionError


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class ProjectAccounts(BaseModel):
    inherit: bool = False
    account_uuids: list[str] = Field(default_factory=list, max_length=10000)


class SceneCreate(BaseModel):
    scene_number: int = Field(..., ge=1, strict=True)
    scene_name: str = Field('', max_length=200)
    prompt: str = ''
    model: str = 'seedance-2.0'
    ratio: str = '16:9'
    duration: int = 10
    start_end: bool = False


class ScenePatch(BaseModel):
    scene_number: int | None = Field(None, ge=1, strict=True)
    scene_name: str | None = Field(None, max_length=200)
    prompt: str | None = None
    model: str | None = None
    ratio: str | None = None
    duration: int | None = None
    start_end: bool | None = None


class ReferenceAttach(BaseModel):
    asset_id: str
    position: int = Field(..., ge=1, strict=True)
    reference_alias: str = Field(..., min_length=1, max_length=200)


class ReferenceOrder(BaseModel):
    asset_ids: list[str]


class ReferenceAlias(BaseModel):
    reference_alias: str = Field(..., min_length=1, max_length=200)


class SelectedGeneration(BaseModel):
    task_id: str = Field(..., min_length=1, strict=True)


class SceneGenerate(BaseModel):
    count: int = Field(1, ge=1, le=5, strict=True)
    request_id: str | None = Field(None, min_length=8, max_length=64, strict=True, pattern=r'^[A-Za-z0-9_-]+$')


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except ImportValidationError as exc:
        raise HTTPException(exc.status_code, exc.report) from exc
    except SceneNotReadyError as exc:
        raise HTTPException(exc.status_code, {key:exc.details[key] for key in ('readiness','missing_references','validation_errors')}) from exc
    except AssetNameCollisionError as exc:
        raise HTTPException(409, {'message':'Ambiguous asset names','collisions':exc.collisions}) from exc
    except RecordNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (AssetInUseError, sqlite3.IntegrityError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        logging.getLogger('uvicorn.error').exception('Persistent reference operation failed')
        raise HTTPException(500, 'Persistent reference storage failed; operation was rolled back') from exc


def register_asset_routes(app, store, admin_auth, client_auth, request_factory, submit_generation):
    router = APIRouter(prefix='/api/admin', tags=['Persistent references'])
    library = AssetLibrary(store)
    library.recover_deletions()

    @router.post('/projects', status_code=201)
    def create_project(body: ProjectCreate, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.create_project, body.name)

    @router.get('/projects')
    def list_projects(x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'projects': store.list_projects()}

    @router.get('/projects/{project_id}/accounts')
    def project_accounts(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.project_accounts, project_id)

    @router.put('/projects/{project_id}/accounts')
    def set_project_accounts(project_id: str, body: ProjectAccounts, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.set_project_accounts, project_id, body.account_uuids, inherit=body.inherit)

    @router.post('/projects/{project_id}/chapters', status_code=201)
    def create_chapter(project_id: str, body: ProjectCreate, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.create_chapter, project_id, body.name)

    @router.get('/projects/{project_id}/chapters')
    def list_chapters(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'chapters': _call(store.list_chapters, project_id)}

    @router.get('/projects/{project_id}')
    def get_project(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(404, 'Project not found')
        return project

    @router.patch('/projects/{project_id}')
    def update_project(project_id: str, body: ProjectCreate, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.update_project, project_id, name=body.name)

    @router.post('/projects/{project_id}/scenes', status_code=201)
    def create_scene(project_id: str, body: SceneCreate, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.create_scene, project_id, **body.model_dump())

    @router.get('/projects/{project_id}/scenes')
    def list_scenes(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'scenes': _call(store.project_scene_details, project_id)}

    @router.get('/scenes/{scene_id}')
    def get_scene(scene_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.scene_details, scene_id)

    @router.patch('/scenes/{scene_id}')
    def update_scene(scene_id: str, body: ScenePatch, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        _call(store.update_scene, scene_id, **body.model_dump(exclude_unset=True))
        return _call(store.scene_details, scene_id)

    @router.post('/projects/{project_id}/scenes/import/validate')
    def validate_import(project_id: str, payload: Any = Body(default=None), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.validate_scene_import, project_id, payload)

    @router.post('/projects/{project_id}/scenes/import', status_code=201)
    def import_scenes(project_id: str, payload: Any = Body(default=None), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.import_scenes, project_id, payload)

    @router.post('/projects/{project_id}/scene-info/import/validate')
    def validate_scene_info(project_id: str, payload: Any = Body(default=None), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.validate_scene_info, project_id, payload)

    @router.post('/projects/{project_id}/scene-info/import')
    def import_scene_info(project_id: str, payload: Any = Body(default=None), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.import_scene_info, project_id, payload)

    @router.patch('/scenes/{scene_id}/summary')
    def update_scene_summary(scene_id: str, payload: Any = Body(default=None), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.update_scene_summary, scene_id, payload)

    @router.get('/scenes/{scene_id}/references')
    def list_requirements(scene_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'references': _call(store.list_scene_requirements, scene_id)}

    @router.post('/scenes/{scene_id}/references/resolve')
    def resolve_requirements(scene_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.resolve_scene_references, scene_id)

    @router.post('/projects/{project_id}/assets', status_code=201)
    async def upload_asset(project_id: str, name: str = Form(..., min_length=1, max_length=200),
                           asset_type: str = Form(..., alias='type'), file: UploadFile = File(...),
                           x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        data = await file.read(config.REFERENCE_IMAGE_MAX_BYTES + 1)
        return await asyncio.to_thread(_call, library.upload, project_id, name, asset_type, file.filename, data)

    @router.get('/projects/{project_id}/assets')
    def list_assets(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'assets': _call(store.list_project_assets, project_id)}

    @router.get('/assets/{asset_id}')
    def get_asset(asset_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        asset = store.get_asset(asset_id)
        if asset is None:
            raise HTTPException(404, 'Asset not found')
        return asset

    @router.get('/assets/{asset_id}/image')
    def asset_image(asset_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        asset = store.get_asset(asset_id)
        if asset is None:
            raise HTTPException(404, 'Asset not found')
        try:
            validate_asset_path(asset['project_id'], asset['id'], asset['file_path'])
            source = resolve_asset_path(asset['file_path'])
        except (ValueError, OSError):
            raise HTTPException(404, 'Reference image is unavailable')
        if not source.is_file():
            raise HTTPException(404, 'Reference image is unavailable')
        media = {'.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.png':'image/png', '.webp':'image/webp'}
        return FileResponse(source, media_type=media[source.suffix.lower()],
                            headers={'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff'})

    @router.post('/assets/{asset_id}/replace')
    async def replace_asset_image(asset_id: str, file: UploadFile = File(...),
                                  x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        data = await file.read(config.REFERENCE_IMAGE_MAX_BYTES + 1)
        return await asyncio.to_thread(_call, library.replace, asset_id, file.filename, data)

    @router.post('/assets/{asset_id}/remove')
    async def remove_current_asset(asset_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return await asyncio.to_thread(_call, library.remove, asset_id)

    @router.delete('/assets/{asset_id}')
    def delete_asset(asset_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        _call(library.delete, asset_id)
        return {'ok': True}

    @router.get('/scenes/{scene_id}/assets')
    def scene_assets(scene_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'assets': _call(store.list_scene_assets, scene_id)}

    @router.post('/scenes/{scene_id}/assets', status_code=201)
    def attach(scene_id: str, body: ReferenceAttach, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'assets': _call(store.attach_asset_to_scene, scene_id, body.asset_id, body.position, body.reference_alias)}

    @router.put('/scenes/{scene_id}/assets/order')
    def reorder(scene_id: str, body: ReferenceOrder, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'assets': _call(store.reorder_scene_assets, scene_id, body.asset_ids)}

    @router.patch('/scenes/{scene_id}/assets/{asset_id}')
    def update_alias(scene_id: str, asset_id: str, body: ReferenceAlias, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return {'assets': _call(store.update_scene_asset_alias, scene_id, asset_id, body.reference_alias)}

    @router.delete('/scenes/{scene_id}/assets/{asset_id}')
    def detach(scene_id: str, asset_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        if not _call(store.detach_asset_from_scene, scene_id, asset_id):
            raise HTTPException(404, 'Asset is not attached to this scene')
        return {'ok': True}

    @router.post('/scenes/{scene_id}/generate', status_code=202)
    async def generate(scene_id: str, body: SceneGenerate | None = None,
                       x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        client = client_auth(authorization)
        scene, references = await asyncio.to_thread(_call, store.scene_generation_input, scene_id)
        req, snapshot = _call(prepare_scene_request, scene, references, request_factory, body.count if body else 1)
        try:
            return await submit_generation(req, client, scene_id=scene_id, reference_snapshot=snapshot,
                                           scene_updated_at=scene['updated_at'], generation_request_id=body.request_id if body else None)
        except RecordNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post('/projects/{project_id}/generate', status_code=202)
    async def generate_project(project_id: str, body: SceneGenerate | None = None, x_admin_key: str | None = Header(default=None),
                               authorization: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        client = client_auth(authorization)
        return await submit_generation(None, client, project_id=project_id, candidates_per_scene=body.count if body else 1,
                                       generation_request_id=body.request_id if body else None)

    @router.get('/projects/{project_id}/generation-status')
    def project_status(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.project_review_status, project_id)

    @router.get('/scenes/{scene_id}/generations')
    def generations(scene_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.scene_generations, scene_id)

    @router.put('/scenes/{scene_id}/selected-generation')
    def select_generation(scene_id: str, body: SelectedGeneration, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.select_scene_task, scene_id, body.task_id)

    @router.delete('/scenes/{scene_id}/selected-generation')
    def clear_generation(scene_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.clear_scene_selection, scene_id)

    @router.get('/projects/{project_id}/selected-videos')
    def selected_videos(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.project_selected_videos, project_id)

    @router.post('/projects/{project_id}/select-latest-completed')
    def select_latest(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return _call(store.select_latest_completed, project_id)

    app.include_router(router)
    return library

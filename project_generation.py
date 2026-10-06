"""Generate All orchestration; snapshots and execution reuse the scene submission path."""
import asyncio
import sqlite3
from fastapi import HTTPException
from production_store import RecordNotFoundError
from scene_generation import prepare_scene_request
from store import TaskQuotaExceeded, PendingTaskLimitExceeded


async def submit_project_generation(project_id, client, store, request_factory, submit, run, max_pending, candidates_per_scene=1, request_id=None):
    try:
        scenes = await asyncio.to_thread(store.project_generation_status, project_id)
        plans = []
        for scene in scenes:
            if scene['skip_reason']:
                continue
            current, refs = await asyncio.to_thread(store.scene_generation_input, scene['id'])
            req, snapshot = prepare_scene_request(current, refs, request_factory, candidates_per_scene)
            plan = await submit(req, client, scene_id=scene['id'], reference_snapshot=snapshot, prepare_only=True)
            plan['scene_updated_at'] = current['updated_at']
            plans.append(plan)
        result, accepted = store.create_project_batch(project_id, plans, client, max_pending, candidates_per_scene, request_id)
    except RecordNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (TaskQuotaExceeded, PendingTaskLimitExceeded) as exc:
        raise HTTPException(429, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise HTTPException(500, 'Project task creation failed; the whole batch was rolled back') from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from exc
    # Only committed tasks are scheduled. Runtime failures are independent.
    for plan in accepted:
        asyncio.create_task(run(plan['task_id'], plan['model'], plan['prompt'], plan['ratio'],
                                plan['duration'], plan['reference_images'], client))
    return result

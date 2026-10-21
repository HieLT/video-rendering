"""Integration tests using temporary databases and the real scheduler input resolver."""
import ast
import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from image_library import ImageLibrary
from store import TaskStore, TaskQuotaExceeded, PendingTaskLimitExceeded, TaskSubmissionConflict
from video_schedule import VideoScheduler
from reference_aliases import resolve_reference_aliases
from test_image_library import png, ROOT

class SharedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.lib=ImageLibrary(self.root/'library')
        self.store=TaskStore(str(self.root/'tasks.db'))
        self.scheduler=VideoScheduler(self.store,SimpleNamespace())
        self.red=self.lib.save(png(),'red.png',10000)['id']
        self.blue=self.lib.save(png('blue'),'blue.png',10000)['id']
        client=dict(allowed_durations=[10,15,30],api_key_hash=None,api_key_name=None,daily_limit=0,concurrency_limit=0)
        self.ns=dict(app=FastAPI(),Header=Header,HTTPException=HTTPException,BaseModel=BaseModel,Field=Field,
            Path=Path,json=json,uuid=uuid,asyncio=asyncio,shutil=shutil,sqlite3=sqlite3,TaskSubmissionConflict=TaskSubmissionConflict,
            image_library=self.lib,store=self.store,UPLOADED_REFERENCES={},
            _admin_auth=lambda key: None,_auth=lambda key:client,
            _run_task=AsyncMock(),SUPPORTED_DURATIONS=(10,15,30),
            config=SimpleNamespace(MAX_PENDING_TASKS=100,REFERENCE_IMAGE_MAX_COUNT=30),pool=SimpleNamespace(available=True,accounts=['a']),
            resolve_reference_aliases=resolve_reference_aliases,
            TaskQuotaExceeded=TaskQuotaExceeded,PendingTaskLimitExceeded=PendingTaskLimitExceeded)
        names={'VideoGenRequest','TaskResponse','BatchTaskResponse','create_video','_submit_video','_resolve_ratio','delete_library_image'}
        tree=ast.parse((ROOT/'server.py').read_text(encoding='utf-8'))
        exec(compile(ast.Module(body=[n for n in tree.body if getattr(n,'name',None) in names],type_ignores=[]),'routes','exec'),self.ns)

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.store._conn.close()
        self.tmp.cleanup()

    async def test_batch_shared_order_duplicates_and_delete_protection(self):
        refs=['library://'+i for i in [self.red,self.blue,self.red]]
        request=self.ns['VideoGenRequest'](prompt='Keep this',reference_images=refs,count=2,start_end=True,has_end_frame=True)
        batch=await self.ns['create_video'](request,None)
        for task in batch.tasks:
            row=self.store.get(task.id)
            self.assertEqual(json.loads(row['reference_images']),refs)
            with patch('video_schedule.ImageLibrary',return_value=self.lib):
                paths=await self.scheduler.references(row,{})
            self.assertEqual([Path(p).read_bytes() for p in paths],[png(),png('blue'),png()])
            self.assertTrue(all(Path(p).parent==self.lib.root for p in paths))
        self.assertEqual(len(list(self.lib.root.glob('*.png'))),2)
        with self.assertRaises(HTTPException) as used:
            await self.ns['delete_library_image'](self.red,None)
        self.assertEqual(used.exception.status_code,409)
        for task in batch.tasks:
            self.store.update(task.id,status='completed')
        with self.assertRaises(HTTPException):
            await self.ns['delete_library_image'](self.red,None)
        for task in batch.tasks:self.store.delete_task(task.id)
        await self.ns['delete_library_image'](self.red,None)
        with self.assertRaises(ValueError):self.lib.path(self.red)
        self.assertTrue(self.lib.path(self.blue).is_file())
        with self.assertRaises(HTTPException) as missing:
            await self.ns['create_video'](request,None)
        self.assertEqual(missing.exception.status_code,422)

    async def test_legacy_and_url_inputs_become_durable_shared_files(self):
        old=self.root/'old.png';old.write_bytes(png())
        for name,sources in [('legacy',['local://'+str(old)]),('url',['https://example.test/image.png'])]:
            self.store.create(name,'seedance-2.5','Keep this','16:9',10)
            self.store.update(name,reference_images=json.dumps(sources))
            remote=self.root/'download';remote.mkdir(exist_ok=True)
            download=remote/'image.png';download.write_bytes(png())
            with patch('video_schedule.ImageLibrary',return_value=self.lib), patch('media.download_reference_images',AsyncMock(return_value=(remote,[str(download)]))):
                paths=await self.scheduler.references(self.store.get(name),{})
            self.assertEqual(paths,[str(self.lib.path(self.red))])
            self.assertEqual(json.loads(self.store.get(name)['reference_images']),['library://'+self.red])
        self.assertFalse(remote.exists())

    async def test_migration_validates_copies_preserves_prompt_and_cleans_only_duplicates(self):
        from migrate_shared_images import migrate
        media=self.root/'job_media'
        folder=media/'old'
        upload=folder/'upload';upload.mkdir(parents=True)
        source=upload/'image.png';source.write_bytes(png())
        runtime=folder/'1.png';runtime.write_bytes(png())
        other=folder/'unrelated.txt';other.write_text('keep')
        self.store.create('old','seedance-2.5','Exact prompt','16:9',10)
        self.store.update('old',reference_images=json.dumps(['local://'+str(source)]),reference_paths=json.dumps([str(runtime)]))
        self.store.create('missing','seedance-2.5','Missing prompt','16:9',10)
        self.store.update('missing',reference_images=json.dumps(['local://'+str(folder/'missing.png')]))
        result=migrate(self.store._conn,self.lib,media,self.root/'audit',True)
        self.assertEqual(result['migrated'],1)
        self.assertEqual(len(result['skipped']),1)
        self.assertEqual(result['removed_files'],2)
        self.assertTrue(other.exists())
        self.assertFalse(source.exists())
        self.assertFalse(runtime.exists())
        self.assertEqual(self.store.get('old')['prompt'],'Exact prompt')
        self.assertEqual(json.loads(self.store.get('old')['reference_images']),['library://'+self.red])
        self.assertTrue((self.root/'audit'/'tasks.before.db').is_file())

    async def test_delete_auth_and_unknown_image(self):
        def denied(key):raise HTTPException(401,'denied')
        self.ns['_admin_auth']=denied
        with self.assertRaises(HTTPException) as error:
            await self.ns['delete_library_image'](self.red,None)
        self.assertEqual(error.exception.status_code,401)

if __name__=='__main__':unittest.main()

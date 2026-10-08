"""One-time, guarded conversion of the four existing Darius/Garen projects."""
import sqlite3, time, uuid, hashlib, shutil, json
from pathlib import Path
import config
from asset_storage import resolve_asset_path, asset_relative_path
from scene_import import asset_name_key

def main():
    db=Path(config.DB_PATH).resolve()
    conn=sqlite3.connect(db,timeout=30);conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    names=['darius_garen_chapter'+n for n in ('1+2','3+4','5+6','7+8')]
    parents=conn.execute("SELECT * FROM projects WHERE name='Darius_Garen'").fetchall()
    assert len(parents)==1,'Expected exactly one target project'
    parent=dict(parents[0]);assert not parent['parent_project_id']
    projects=[]
    for name in names:
        rows=conn.execute('SELECT * FROM projects WHERE name=?',(name,)).fetchall()
        assert len(rows)==1,'Missing or duplicate source '+name
        row=dict(rows[0]);assert row['parent_project_id'] is None
        projects.append(row)
    ids=[p['id'] for p in projects];marks=','.join('?' for _ in ids)
    assert not conn.execute(f"SELECT 1 FROM tasks t JOIN scenes s ON s.id=t.scene_id WHERE s.project_id IN ({marks}) AND t.status IN ('queued','processing')",ids).fetchone(),'Source tasks are active'
    assert not conn.execute('SELECT 1 FROM assets WHERE project_id=?',(parent['id'],)).fetchone(),'Target library must be empty'
    assert not conn.execute(f'SELECT 1 FROM project_account_allowlist WHERE project_id IN ({marks})',ids).fetchone(),'Source account restrictions require reconciliation'
    backup=db.with_name(db.name+'.before_darius_chapters_'+time.strftime('%Y%m%d_%H%M%S')+'.bak')
    with sqlite3.connect(backup) as target:conn.backup(target)
    originals=[dict(r) for r in conn.execute(f'SELECT * FROM assets WHERE project_id IN ({marks}) AND retired_at IS NULL ORDER BY created_at,id',ids)]
    before_scenes=[tuple(r) for r in conn.execute(f'SELECT * FROM scenes WHERE project_id IN ({marks}) ORDER BY id',ids)]
    before_tasks=[tuple(r) for r in conn.execute(f'SELECT t.* FROM tasks t JOIN scenes s ON s.id=t.scene_id WHERE s.project_id IN ({marks}) ORDER BY t.id',ids)]
    canonical={};mapping={};new_assets=[];copies=[];variants=[]
    for old in originals:
        source=resolve_asset_path(old['file_path']);blob=source.read_bytes();digest=hashlib.sha256(blob).hexdigest()
        key=asset_name_key(old['name']);match=(key,digest,old['type'])
        if match not in canonical:
            name=old['name']
            if any(k[0]==key for k in canonical):
                chapter=next(p['name'].removeprefix('darius_garen_') for p in projects if p['id']==old['project_id'])
                name+=' ['+chapter+']';variants.append(name)
            new_id=uuid.uuid4().hex
            relative=asset_relative_path(parent['id'],new_id,source.suffix)
            canonical[match]=(new_id,name,relative)
            new_assets.append((new_id,parent['id'],name,old['type'],relative,time.time()))
            copies.append((source,resolve_asset_path(relative),digest))
        mapping[old['id']]=canonical[match]
    for source,target,digest in copies:
        target.parent.mkdir(parents=True,exist_ok=True)
        assert not target.exists()
        shutil.copy2(source,target)
        assert hashlib.sha256(target.read_bytes()).hexdigest()==digest
    try:
        conn.execute('BEGIN IMMEDIATE')
        assert not conn.execute(f"SELECT 1 FROM tasks t JOIN scenes s ON s.id=t.scene_id WHERE s.project_id IN ({marks}) AND t.status IN ('queued','processing')",ids).fetchone(),'Tasks started during preparation'
        assert [tuple(r) for r in conn.execute(f'SELECT * FROM scenes WHERE project_id IN ({marks}) ORDER BY id',ids)]==before_scenes
        assert [tuple(r) for r in conn.execute(f'SELECT t.* FROM tasks t JOIN scenes s ON s.id=t.scene_id WHERE s.project_id IN ({marks}) ORDER BY t.id',ids)]==before_tasks
        conn.executemany('INSERT INTO assets(id,project_id,name,type,file_path,created_at) VALUES (?,?,?,?,?,?)',new_assets)
        for old_id,(new_id,name,relative) in mapping.items():
            conn.execute('UPDATE scene_reference_requirements SET asset_id=?,name=? WHERE asset_id=?',(new_id,name,old_id))
            conn.execute('UPDATE scene_assets SET asset_id=? WHERE asset_id=?',(new_id,old_id))
            # Preserve historical snapshots and their original files/asset identities.
            conn.execute('UPDATE assets SET retired_at=? WHERE id=?',(time.time(),old_id))
        for project in projects:
            conn.execute('UPDATE projects SET parent_project_id=?,name=?,updated_at=? WHERE id=?',(parent['id'],project['name'].removeprefix('darius_garen_'),time.time(),project['id']))
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert [tuple(r) for r in conn.execute(f'SELECT * FROM scenes WHERE project_id IN ({marks}) ORDER BY id',ids)]==before_scenes
        assert [tuple(r) for r in conn.execute(f'SELECT t.* FROM tasks t JOIN scenes s ON s.id=t.scene_id WHERE s.project_id IN ({marks}) ORDER BY t.id',ids)]==before_tasks
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:conn.close()
    from store import TaskStore
    store=TaskStore(str(db))
    try:
        for project in projects:
            for scene in store.project_scene_details(project['id']):
                assert not scene['validation_errors'],scene['validation_errors']
                for ref in store.list_scene_assets(scene['id']):
                    assert ref['project_id']==parent['id']
        print(json.dumps(dict(chapters=4,scenes=len(before_scenes),tasks_preserved=len(before_tasks),shared_assets=len(new_assets),variants=variants,backup=str(backup)),ensure_ascii=True))
    finally:store._conn.close()

if __name__=='__main__':main()

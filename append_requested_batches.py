"""Append the 65 videos requested on 2026-10-15 through the running server.

Default is preflight only. --execute sends each request once. An on-disk journal
blocks a repeat submission, including after an uncertain HTTP outcome.
"""
import argparse
import json
import sqlite3
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
BATCHES = '''1360f45d15164587a851e93bb4069ead
54d67a33a26f4267a3a3a2ea2275a0e7
4ba64b54cf334c3e92172a241fd278d6
16b735ae190043b88a462d7672e6bc72
fc4151ce21964f4e886e6877ba0173fc
e9d882315b2b4398afc8b634ff17cc91
1e7fc5c373944a839acc39b5b491443a
961f895099d845ebb6cf4930e21f9eb3
45ac38ab35c8498ab64c647a79f948c8
0d315d057eec44fdaf64b3679f353de8
fb0fadeec99945968415776db47f0a4d
e614ba8bd487496aab2c7bdc0206181c
d269f7cbd22b420095bce13c0ee6fde7'''.split()
FIELDS = ('model', 'prompt', 'ratio', 'duration', 'start_end', 'reference_images')
JOURNAL = ROOT / 'exports' / 'append_65_20261015.json'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    db = sqlite3.connect((ROOT / 'tasks.db').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    require(not JOURNAL.exists(), 'Journal already exists; inspect it before any further submission.')
    plan = []
    for suffix in BATCHES:
        batch_id = 'batch_' + suffix
        rows = [dict(r) for r in db.execute(
            'SELECT * FROM tasks WHERE batch_id=? ORDER BY batch_index,id', (batch_id,))]
        require(len(rows) == 5, f'{batch_id}: expected exactly 5 existing items')
        first = rows[0]
        require(not first['start_end'] and not first['scene_id'] and not first['api_key_hash'],
                f'{batch_id}: unsupported source context')
        require(all(not r['deleted_at'] and all(r[k] == first[k] for k in FIELDS) for r in rows),
                f'{batch_id}: original items differ')
        refs = json.loads(first['reference_images'])
        for ref in refs:
            require(ref.startswith('library://'), 'Expected persistent library reference')
            files = list((ROOT / '.image_library').glob(ref[10:] + '.*'))
            require(any(p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp') and p.is_file()
                        for p in files), f'Missing library image: {ref}')
        payload = {k: first[k] for k in ('model', 'prompt', 'ratio', 'duration', 'name')}
        payload.update(batch_id=batch_id, count=5, start_end=False, reference_images=refs)
        plan.append(dict(batch_id=batch_id, name=first['batch_name'], source_id=first['id'],
                         original_ids=[r['id'] for r in rows], payload=payload, state='pending'))
    print('Preflight passed: 13 batches, 65 new videos; all source configurations and images checked.')
    if not args.execute:
        return
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open('x', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    def save():
        temp = JOURNAL.with_suffix('.tmp')
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(JOURNAL)

    for entry in plan:
        entry['state'] = 'sending'
        save()
        request = Request('http://127.0.0.1:8000/v1/videos/generations',
                          data=json.dumps(entry['payload']).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=120) as response:
                result = json.load(response)
            entry['response'] = result
            save()
            ids = [t['id'] for t in result['tasks']]
            require(len(ids) == 5 and len(set(ids)) == 5, 'Expected five unique new task IDs')
            require(result['batch_id'] == entry['batch_id'], 'Wrong destination batch')
            first = dict(db.execute('SELECT * FROM tasks WHERE id=?', (entry['source_id'],)).fetchone())
            for task_id in ids:
                require(task_id not in entry['original_ids'], 'Returned original task ID')
                row = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
                require(all(row[k] == first[k] for k in FIELDS), 'Persisted configuration differs')
                require(row['batch_id'] == entry['batch_id'] and row['batch_count'] == 10,
                        'Batch membership mismatch')
            all_rows = db.execute('SELECT batch_index,batch_count FROM tasks WHERE batch_id=? ORDER BY batch_index',
                                  (entry['batch_id'],)).fetchall()
            require([r['batch_index'] for r in all_rows] == list(range(1, 11))
                    and all(r['batch_count'] == 10 for r in all_rows), 'Batch numbering mismatch')
            entry['state'] = 'verified'
            save()
            print(f"Verified +5: {entry['name'] or entry['batch_id']}", flush=True)
        except Exception as exc:
            entry['state'] = 'needs_inspection'
            entry['error'] = str(exc)
            save()
            raise
    print('DONE: 65 new tasks persisted and admitted through the normal generation API.')


if __name__ == '__main__':
    main()

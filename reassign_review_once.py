import json, sqlite3, pathlib, urllib.request, subprocess, time, hashlib
import config
ROOT=pathlib.Path(__file__).resolve().parent
IDS=['video_3f9bfed9da0f4091babd26ece3fffb6a','video_e5c35c996e454f1ba7188234612be7e8','video_e226396a1f5d41c093e67f9593820bcb','video_d992bec7efd9478ea9c46274fe5e8c56','video_c6e5de15d2f74b98bfb504191e1841df','video_50173b8171e94fc0ab84352efc4559e0']
def api(path,method='GET',body=None):
    req=urllib.request.Request('http://127.0.0.1:8000'+path,method=method,headers={'x-admin-key':config.ADMIN_KEY or '', 'Content-Type':'application/json'},data=None if body is None else json.dumps(body).encode())
    return json.load(urllib.request.urlopen(req,timeout=20))
def ps(command):
    return subprocess.check_output(['powershell','-NoProfile','-Command',command],text=True)
c=sqlite3.connect(config.DB_PATH); c.row_factory=sqlite3.Row
rows=[dict(c.execute('SELECT * FROM tasks WHERE id=?',(i,)).fetchone()) for i in IDS]
assert all(r['status']=='needs_recovery' and r['deleted_at'] is None for r in rows)
for r in rows:
    paths=json.loads(r['reference_paths'] or '[]')
    assert len(paths)==3 and all(pathlib.Path(p).is_file() for p in paths),r['id']
accounts=api('/api/admin/accounts')['accounts']
idle=[a for a in accounts if a['scheduling'] and a['login_ok']==1 and a['auth_state']=='active' and not any(a[k] for k in ('busy','cooling','rate_limited','quota_blocked')) and a['remaining']>0 and (a['credit_balance'] is None or a['credit_balance']>=2)]
assert len(idle)>=len(rows)
pool=sqlite3.connect('pool_usage.db'); pool.row_factory=sqlite3.Row
mapping=[]
for r,a in zip(rows,idle):
    name=pool.execute('SELECT name FROM accounts_meta WHERE uuid=?',(a['uuid'],)).fetchone()[0]
    assert name!=r['account']
    mapping.append(dict(id=r['id'],old_account=r['account'],account=name,account_uuid=a['uuid']))
stamp=time.strftime('%Y%m%d_%H%M%S')
audit=ROOT/'diagnostics'/('reassign_review_'+stamp+'.json')
audit.parent.mkdir(exist_ok=True)
audit.write_text(json.dumps(dict(originals=rows,mapping=mapping,image_hashes={p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for r in rows for p in json.loads(r['reference_paths'])}),ensure_ascii=False,indent=2),encoding='utf-8')
# Disable dispatch via the running server so its account state stays consistent.
for r in rows:
    api('/api/admin/accounts/'+(r['account_uuid'] or r['account']),'PATCH',{'scheduling':False})
# Only interrupt the gateway when all other active jobs are durably waiting.
active=c.execute("SELECT id,phase FROM tasks WHERE deleted_at IS NULL AND status IN ('queued','processing')").fetchall()
assert all(r['phase']=='waiting' for r in active),[dict(r) for r in active]
for db,name in [(c,'tasks'),(pool,'pool')]:
    dest=sqlite3.connect(str(ROOT/'diagnostics'/(name+'_before_reassign_'+stamp+'.db')))
    db.backup(dest); dest.close()
restart=False
try:
    restart=True
    ps("$ErrorActionPreference='Stop'; $gatewayProcs=Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'uvicorn server:app' -and $_.ProcessId -in @(1384,26040) }; if (@($gatewayProcs).Count -eq 0) { throw 'Gateway process changed' }; $gatewayProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    time.sleep(2)
    c.execute('BEGIN IMMEDIATE')
    for m in mapping:
        changed=c.execute("UPDATE tasks SET account=?,account_uuid=?,status='queued',phase='ready',conversation_id=NULL,deadline_at=NULL,last_poll_at=NULL,failure_code=NULL,next_check_at=NULL,accepted_at=NULL,eta_seconds=NULL,eta_raw=NULL,check_round=0,retry_count=0,after_index=0,dispatch_uncertain=0,submitted_at=NULL,result_url=NULL,video_url=NULL,error=NULL,finished_at=NULL,updated_at=? WHERE id=? AND status='needs_recovery' AND deleted_at IS NULL",(m['account'],m['account_uuid'],time.time(),m['id'])).rowcount
        assert changed==1,m['id']
    c.commit()
    print(json.dumps(dict(reassigned=mapping,audit=str(audit))),flush=True)
except BaseException:
    c.rollback()
    raise
finally:
    if restart:
        ps("Start-Process -FilePath 'D:\\dola-render-gateway\\.venv\\Scripts\\python.exe' -ArgumentList '-m','uvicorn','server:app','--host','0.0.0.0','--port','8000' -WorkingDirectory 'D:\\dola-render-gateway' -RedirectStandardOutput 'D:\\dola-render-gateway\\server_reassign_"+stamp+".log' -RedirectStandardError 'D:\\dola-render-gateway\\server_reassign_"+stamp+".err.log' -WindowStyle Hidden")
for attempt in range(30):
    try:
        api('/api/admin/accounts')
        print('Gateway healthy',flush=True)
        break
    except Exception:
        time.sleep(1)
else:
    raise RuntimeError('Gateway failed health check')

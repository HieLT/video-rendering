"""Offline shared-image migration. Dry run by default; --apply --cleanup after stopping server."""
import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from image_library import ImageLibrary

ROOT = Path(__file__).resolve().parent

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def plan(db, library):
    entries, skipped = [], []
    for raw in db.execute("SELECT id,reference_images,reference_paths FROM tasks"):
        row = dict(raw)
        try:
            sources = json.loads(row["reference_images"] or "[]")
            saved = json.loads(row["reference_paths"] or "[]")
            if not sources and not saved:
                continue
            if sources and all(s.startswith("library://") for s in sources):
                for source in sources:
                    library.path(source[10:])
                continue
            originals = saved or [s[8:] for s in sources if s.startswith("local://")]
            if not originals or (sources and len(originals) != len(sources)):
                raise ValueError("No complete saved input set")
            hashes = [digest(p) for p in originals]
            # Two old copies must agree before either is removed.
            for index, source in enumerate(sources):
                if source.startswith("local://") and Path(source[8:]).is_file():
                    if digest(source[8:]) != hashes[index]:
                        raise ValueError("Saved source and runtime image differ")
            entries.append(dict(row, originals=originals, hashes=hashes))
        except (ValueError, OSError, TypeError) as error:
            skipped.append({"id":row["id"],"reason":str(error)})
    return entries, skipped

def migrate(db, library, media_root, audit_root, cleanup=False):
    entries, skipped = plan(db, library)
    audit_root.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(audit_root/"tasks.before.db") as backup:
        db.backup(backup)
    report={"jobs":entries,"skipped":skipped,"removed_files":[],"removed_bytes":0}
    # Preserve the full old-to-new mapping before modifying the database.
    manifest=audit_root/"manifest.json"
    manifest.write_text(json.dumps(report,indent=2),encoding="utf-8")
    for entry in entries:
        identities=[library.import_path(p)["id"] for p in entry["originals"]]
        if identities != entry["hashes"]:
            raise RuntimeError("Input changed during migration")
        paths=[str(library.path(identity).resolve()) for identity in identities]
        if [digest(p) for p in paths] != identities:
            raise RuntimeError("Library verification failed")
        with db:
            db.execute("UPDATE tasks SET reference_images=?,reference_paths=? WHERE id=?",
                (json.dumps(["library://"+i for i in identities]),json.dumps(paths),entry["id"]))
    if cleanup:
        protected=set()
        for row in db.execute("SELECT reference_images,reference_paths FROM tasks"):
            sources,paths=(json.loads(value or "[]") for value in row)
            protected.update(Path(p).resolve() for p in paths)
            protected.update(Path(s[8:]).resolve() for s in sources if s.startswith("local://"))
        media_root=media_root.resolve()
        for entry in entries:
            folder=(media_root/entry["id"]).resolve()
            if not folder.is_relative_to(media_root) or folder==media_root:
                raise RuntimeError("Unsafe job media folder")
            if not folder.exists():
                continue
            allowed=set(entry["hashes"])
            for candidate in folder.rglob("*"):
                path=candidate.resolve()
                if not path.is_relative_to(media_root) or path in protected or not path.is_file():
                    continue
                identity=digest(path)
                if identity not in allowed or digest(library.path(identity))!=identity:
                    continue
                size=path.stat().st_size
                # Persist mapping before unlink so an interrupted cleanup remains auditable.
                report["removed_files"].append({"path":str(path),"library_id":identity,"size":size})
                report["removed_bytes"]+=size
                manifest.write_text(json.dumps(report,indent=2),encoding="utf-8")
                path.unlink()
            for directory in sorted(folder.rglob("*"),key=lambda p:len(p.parts),reverse=True):
                if directory.is_dir() and not directory.is_symlink():
                    try:directory.rmdir()
                    except OSError:pass
            try:folder.rmdir()
            except OSError:pass
    manifest.write_text(json.dumps(report,indent=2),encoding="utf-8")
    return {"migrated":len(entries),"skipped":skipped,"removed_files":len(report["removed_files"]),
            "removed_bytes":report["removed_bytes"],"audit":str(audit_root)}

def main():
    import config
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--cleanup",action="store_true")
    args=parser.parse_args()
    guard=None
    if args.apply:
        from browser_queue import lock_file,unlock_file,ROOT as runtime
        guard=lock_file(runtime/"scheduler.lock")
        if guard is None:raise SystemExit("Stop the gateway before applying migration")
    try:
        db=sqlite3.connect(str(Path(config.DB_PATH).resolve()) if args.apply else
            Path(config.DB_PATH).resolve().as_uri()+"?mode=ro",uri=not args.apply)
        db.row_factory=sqlite3.Row
        library=ImageLibrary(ROOT/".image_library")
        if args.apply:
            result=migrate(db,library,ROOT/".job_media",
                ROOT/"diagnostics"/("shared_images_"+str(time.time_ns())),args.cleanup)
        else:
            entries,skipped=plan(db,library)
            result={"eligible":len(entries),"skipped":skipped,
                    "unique_images":len({i for e in entries for i in e["hashes"]})}
        result["skipped_count"]=len(result.pop("skipped"))
        print(json.dumps(result))
        db.close()
    finally:
        if guard:unlock_file(guard)

if __name__=="__main__":main()

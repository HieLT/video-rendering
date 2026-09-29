import sqlite3
import re
import os
from pathlib import Path

def sanitize_filename_prefix(name: str) -> str:
    if not name or not name.strip():
        return ""
    clean = re.sub(r'[\\/]', '_', name.strip())
    clean = re.sub(r'[*?:"<>|]', '', clean).strip()
    return f"{clean}_" if clean else ""

def migrate_existing_videos():
    db_path = "tasks.db"
    dl_dir = Path("downloads")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    rows = cur.execute("SELECT id, name, video_url FROM tasks WHERE video_url IS NOT NULL").fetchall()
    renamed_count = 0
    skipped_count = 0
    not_found_count = 0
    locked_count = 0

    print(f"Total tasks with video_url: {len(rows)}")

    for task_id, name, video_url in rows:
        if not name or not name.strip():
            skipped_count += 1
            continue

        prefix = sanitize_filename_prefix(name)
        if not prefix:
            skipped_count += 1
            continue

        # Extract current filename from URL
        old_filename = Path(video_url).name
        
        # Check if already prefixed
        if old_filename.startswith(prefix):
            # Already renamed
            skipped_count += 1
            continue

        new_filename = f"{prefix}{old_filename}"
        old_path = dl_dir / old_filename
        new_path = dl_dir / new_filename

        if not old_path.exists():
            if new_path.exists():
                # File was renamed previously, update DB to match
                new_url = video_url.rsplit('/', 1)[0] + '/' + new_filename
                cur.execute("UPDATE tasks SET video_url = ? WHERE id = ?", (new_url, task_id))
                renamed_count += 1
            else:
                not_found_count += 1
            continue

        # Rename file on disk
        try:
            old_path.rename(new_path)
            new_url = video_url.rsplit('/', 1)[0] + '/' + new_filename
            cur.execute("UPDATE tasks SET video_url = ? WHERE id = ?", (new_url, task_id))
            renamed_count += 1
            print(f"Renamed: {old_filename} -> {new_filename}")
        except PermissionError:
            locked_count += 1
            print(f"[Warning] File in use (please close player to rename): {old_filename}")

    conn.commit()
    conn.close()
    print("\n--- Migration Complete ---")
    print(f"Successfully renamed: {renamed_count}")
    print(f"Skipped (no name or already prefixed): {skipped_count}")
    print(f"File locked / in use: {locked_count}")
    print(f"Not found on disk: {not_found_count}")

if __name__ == "__main__":
    migrate_existing_videos()

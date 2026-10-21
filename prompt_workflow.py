"""Manual AI hand-off workflow. No AI calls and no video/task mutations."""
import copy
import hashlib
import json
from pathlib import Path
import re
import time

from fastapi import Body, Header, HTTPException
from scene_import import ImportValidationError

TEMPLATES = Path(__file__).resolve().parent / "template"
PATTERNS = {
    "t1": "template_1*", "t2": "Template 2*", "t3": "Template 3*",
    "t4": "Template 4*", "t5": "TEMPLATE 5*", "info": "SCENE INFO*",
}
SEP = "=" * 50


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def chapters(text):
    if not re.fullmatch(r"\s*\d+(?:\s*[+,]\s*\d+)*\s*", text or ""):
        raise ValueError("Nhập Chapter bằng số, ví dụ 1 hoặc 1+2 hoặc 1,2.")
    values = sorted(set(int(n) for n in re.findall(r"\d+", text)))
    if not values or min(values) < 1:
        raise ValueError("Số Chapter phải lớn hơn 0.")
    return values


def scene_blocks(text):
    """Only explicit scene headings; never guess by arbitrary numbers in prose."""
    matches = list(re.finditer(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?(?:\*\*)?SCENE\s+(\d+)"
        r"(?=\s*[\u2014\u2013:-](?!\s*\d))[^\n]*", text or ""))
    result = {}
    for i, match in enumerate(matches):
        number = int(match[1])
        if number < 1 or number in result:
            raise ValueError(f"Scene {number} bị trùng hoặc không hợp lệ.")
        result[number] = text[match.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()
    return result


def outline_chapters(text):
    return sorted({int(n) for n in re.findall(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?CHAPTER\s+(\d+)(?=\s|[:—–\-.*]|$)", text)})


def output_json(text):
    for candidate in [text] + re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I):
        # Be tolerant of the common model mistake `,}` / `,]`, without
        # changing commas that occur inside JSON strings.
        cleaned = []
        in_string = escaped = False
        i = 0
        while i < len(candidate):
            char = candidate[i]
            if in_string:
                cleaned.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
                cleaned.append(char)
            elif char == ",":
                lookahead = i + 1
                while lookahead < len(candidate) and candidate[lookahead].isspace():
                    lookahead += 1
                if lookahead < len(candidate) and candidate[lookahead] in "]}":
                    i += 1
                    continue
                cleaned.append(char)
            else:
                cleaned.append(char)
            i += 1
        try:
            value = json.loads("".join(cleaned))
            if isinstance(value, dict) and isinstance(value.get("scenes"), list):
                return value
        except ValueError:
            pass
    raise ValueError("Không tìm được JSON có mảng scenes. Paste JSON riêng hoặc đặt trong khối ```json ... ```.")


def split_sections(text):
    """Suggestion only: users review/edit the extracted fields before approval."""
    cleaned = re.sub(r"(?m)^\s*[=#*_ -]{4,}\s*$", "", text)
    pattern = (r"(?im)^[ \t]*(?:#{1,6}\s*)?(?:\*\*)?"
               r"(?:(?:PART\s+\d+\s*[—–:\-]\s*)|(?:\d+[.)]\s*))?"
               r"(FULL STORY|FULL CHAPTER OUTLINE|CHAPTER DESIGN|STORY STRUCTURE|FILM OVERVIEW|"
               r"REFERENCE PLAN|SCENE\s*(?:→|->|–|—)\s*REFERENCE MAP|"
               r"MISSING VISUAL INFORMATION|MISSING VISUAL DECISIONS|"
               r"FINAL MASTER REFERENCE LIST|PRIORITY|FINAL VALIDATION)"
               r"(?:\*\*)?[ \t]*:?[ \t]*$")
    matches = list(re.finditer(pattern, cleaned))
    result = {}
    for i, match in enumerate(matches):
        key = match[1].upper()
        key = "reference_map" if "REFERENCE MAP" in key else key.lower().replace(" ", "_")
        if key == "chapter_design":
            key = "full_chapter_outline"
        result[key] = cleaned[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)].strip()
        if key == "reference_map" and not result.get("reference_plan"):
            result["reference_plan"] = cleaned[:match.start()].strip()
    return result


def replace_section(template, name, content):
    pattern = r"(={10,}\s*\n" + re.escape(name) + r"\s*\n={10,}\s*\n).*?(?=\n={10,}\s*\n|\Z)"
    output, count = re.subn(pattern, lambda m: m[1] + "\n" + content + "\n", template, count=1, flags=re.S)
    if count == 1:
        return output

    # Newer prompt files use Markdown headings with placeholder text.
    heading = re.search(r"(?m)^(#{1,6})[ \t]+" + re.escape(name) + r"[ \t]*\r?$", template)
    if heading:
        level = len(heading.group(1))
        start = template.find("\n", heading.end()) + 1
        end = len(template)
        for following in re.finditer(r"(?m)^(#{1,6})[ \t]+", template[start:]):
            if len(following.group(1)) <= level:
                end = start + following.start()
                break
        return template[:start] + "\n" + content + "\n\n" + template[end:]

    raise ValueError(f"Template section {name!r} was not found; check the template file.")

class PromptWorkflow:
    def __init__(self, store):
        self.store = store
        with store._lock, store._conn:
            store._conn.execute("""CREATE TABLE IF NOT EXISTS prompt_workflows (
                project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL, data TEXT NOT NULL)""")

    def read(self, project_id):
        # Called with the store's non-reentrant lock held.
        owner = self.store._library_project_id(project_id)
        row = self.store._conn.execute("SELECT revision,data FROM prompt_workflows WHERE project_id=?", (owner,)).fetchone()
        return owner, row[0] if row else 0, json.loads(row[1]) if row else {}

    def assets(self, owner):
        rows = self.store._conn.execute(
            "SELECT * FROM assets WHERE project_id=? AND retired_at IS NULL ORDER BY created_at,id", (owner,))
        return [{"name": a["name"], "type": a["type"], "id": a["id"],
                 "file_path": a["file_path"]} for a in rows]

    def key(self, stage, fields):
        if stage not in PATTERNS:
            raise ValueError("Template không hợp lệ.")
        return stage + (":" + "+".join(map(str, chapters(fields.get("chapters", "")))) if stage in ("t3", "t5") else "")

    def require(self, data, key, owner, seen=()):
        entry = data.get(key, {}).get("approved")
        if not entry:
            raise ValueError(f"Chưa có kết quả đã duyệt: {key}.")
        if key in seen:
            raise ValueError("Dữ liệu phụ thuộc không hợp lệ.")
        source = self.source(data, key.split(":")[0], entry["fields"], owner, seen + (key,))
        if digest(source) != entry["source_hash"]:
            raise ValueError(f"{key}: nguồn đã thay đổi; hãy kiểm tra và duyệt lại.")
        return entry

    def plans(self, data, owner, selected=None, seen=()):
        result = []
        for key in data:
            if not key.startswith("t3:") or not data[key].get("approved"):
                continue
            group = chapters(key[3:])
            if selected is not None:
                overlap = set(group) & set(selected)
                if not overlap:
                    continue
                if not set(group) <= set(selected):
                    raise ValueError(f"Scene Plan được lưu theo nhóm {key[3:]}; hãy chọn đủ nhóm này.")
            entry = self.require(data, key, owner, seen)
            result.append((group, entry["fields"]["output"]))
        result.sort(key=lambda item: item[0])
        if not result:
            raise ValueError("Chưa có Scene Plan đã duyệt.")
        if selected is not None and set(sum((g for g, _ in result), [])) != set(selected):
            raise ValueError("Chưa có đủ Scene Plan đã duyệt cho nhóm Chapter đã chọn.")
        if selected is None:
            outline = self.require(data, "t2", owner, seen)["fields"]["outline"]
            expected = set(outline_chapters(outline))
            actual = set(sum((g for g, _ in result), []))
            if expected and expected != actual:
                raise ValueError(f"Scene Plan chưa khớp toàn bộ Outline. Chapter thiếu: {sorted(expected-actual)}; dư: {sorted(actual-expected)}.")
        return result

    @staticmethod
    def scene_start(fields, selected):
        raw = fields.get("scene_start", "").strip()
        if raw:
            if not raw.isdigit() or int(raw) < 1:
                raise ValueError("S\u1ed1 Scene b\u1eaft \u0111\u1ea7u ph\u1ea3i l\u00e0 s\u1ed1 nguy\u00ean l\u1edbn h\u01a1n 0.")
            return int(raw)
        # Preserve existing saved plans by reading only their first explicit Scene heading.
        match = re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?SCENE\s+(\d+)(?=\s*[\u2014\u2013:-](?!\s*\d))", fields.get("output", ""))
        if match:
            return int(match.group(1))
        if min(selected) == 1:
            return 1
        raise ValueError("Nh\u1eadp s\u1ed1 Scene b\u1eaft \u0111\u1ea7u cho nh\u00f3m n\u00e0y, v\u00ed d\u1ee5 17.")

    def source(self, data, stage, fields, owner, seen=()):
        if stage == "t1":
            return {k: fields.get(k, "").strip() for k in ("champion", "minutes")}
        if stage == "t2":
            return {k: fields.get(k, "").strip() for k in ("idea", "minutes")}
        if stage == "info":
            return {"plans": self.plans(data, owner, seen=seen)}
        story = self.require(data, "t2", owner, seen)["fields"]["story"]
        if stage == "t3":
            selected = chapters(fields.get("chapters", ""))
            expected = outline_chapters(self.require(data, "t2", owner, seen)["fields"]["outline"])
            if expected and not set(selected) <= set(expected):
                raise ValueError("Chapter được chọn không có trong Full Chapter Outline.")
            return {"story": story, "outline": self.require(data, "t2", owner, seen)["fields"]["outline"],
                    "chapters": selected, "start_scene_number": self.scene_start(fields, selected)}
        if stage == "t4":
            return {"story": story, "plans": self.plans(data, owner, seen=seen)}
        selected = chapters(fields.get("chapters", ""))
        plans = self.plans(data, owner, selected, seen)
        t4_fields = self.require(data, "t4", owner, seen)["fields"]
        return {"story": story, "plans": plans,
                # Pass the approved source text verbatim. The prompt model can
                # match map entries to the selected plan without code parsing.
                "reference_map": t4_fields["reference_map"],
                "assets": self.assets(owner)}

    @staticmethod
    def plan_text(plans):
        return "\n\n".join(f"CHAPTER(S) {'+'.join(map(str, group))}\n\n{text}" for group, text in plans)

    def generate(self, project_id, stage, fields):
        with self.store._lock:
            owner, revision, data = self.read(project_id)
            self.key(stage, fields)
            source = self.source(data, stage, fields, owner)
            files = list(TEMPLATES.glob(PATTERNS[stage]))
            if len(files) != 1:
                raise ValueError("Không tìm thấy đúng một file template.")
            template = files[0].read_text(encoding="utf-8-sig")
            if stage == "t1":
                if not source["champion"] or not source["minutes"]:
                    raise ValueError("Nhập tên tướng và thời lượng phim.")
                template = template.replace("[TÊN NHÂN VẬT]", source["champion"], 1)
                template = template.replace("[VÍ DỤ: khoảng 20 phút]", source["minutes"] + " phút", 1)
                template = template.replace("[VÍ DỤ: realistic cinematic live-action, dark fantasy]", "", 1)
            elif stage == "t2":
                if not source["idea"] or not source["minutes"]:
                    raise ValueError("Nhập ý tưởng đã chọn và thời lượng phim.")
                template = replace_section(template, "SELECTED FILM IDEA", source["idea"])
                template = replace_section(template, "TARGET FILM LENGTH", source["minutes"] + " phút")
            else:
                sections = {}
                if stage != "info":
                    sections["FULL STORY"] = source["story"]
                if stage == "t3":
                    sections.update({"FULL CHAPTER OUTLINE": source["outline"],
                                     "CHAPTER(S) TO DEVELOP": "\n".join(f"CHAPTER {n}" for n in source["chapters"]),
                                     "GLOBAL SCENE NUMBERING": (
                                         f"Start the first Scene at SCENE {source['start_scene_number']}. "
                                         "Number every following Scene consecutively and globally across all requested Chapters. "
                                         "Never restart numbering at a new Chapter. Use each Scene number exactly once."
                                     )})
                elif stage == "t4":
                    sections["ALL APPROVED SCENE PLANS"] = self.plan_text(source["plans"])
                elif stage == "t5":
                    sections["APPROVED SCENE PLAN"] = "\n\n".join(text for _, text in source["plans"])
                else:
                    sections["APPROVED SCENE PLAN"] = self.plan_text(source["plans"])
                if stage == "t5":
                    sections["REFERENCE PLAN"] = source["reference_map"]
                    sections["AVAILABLE REFERENCES"] = (
                        "\n".join(f"- {a['name']} ({a['type']})" for a in source["assets"]) or "(No references available)"
                    )
                    sections["GLOBAL VIDEO STYLE"] = ""
                    # Keep the template's default settings, without user overrides.
                for name, value in sections.items():
                    template = replace_section(template, name, value)
            return {"prompt": template, "source_hash": digest(source), "revision": revision,
                    "sources": source, "template": files[0].name}

    def validate_approval(self, stage, fields, data, key, owner):
        if stage != "t4" and not fields.get("output", "").strip():
            raise ValueError("Paste kết quả AI trước khi duyệt.")
        required = {"t2": ("story", "outline"),
                    "t4": ("reference_list", "reference_map", "missing_decisions")}
        for name in required.get(stage, ()):
            if not fields.get(name, "").strip():
                raise ValueError(f"Thiếu phần {name}; hãy tách và kiểm tra kết quả AI.")
        if stage == "t3":
            selected = set(chapters(fields.get("chapters", "")))
            new_scenes = scene_blocks(fields["output"])
            if not new_scenes:
                raise ValueError("Scene Plan needs at least one Scene.")
            numbers = sorted(new_scenes)
            if numbers != list(range(numbers[0], numbers[-1] + 1)):
                raise ValueError("Scene Plan scene numbers must be consecutive.")

            earlier_scenes, later_scenes = [], []
            for existing_key, record in data.items():
                if existing_key == key or not existing_key.startswith("t3:"):
                    continue
                approved = record.get("approved")
                if not approved:
                    continue
                existing_group = set(chapters(existing_key[3:]))
                overlap = selected & existing_group
                if overlap:
                    raise ValueError(
                        f"Chapter {sorted(overlap)} overlaps an approved Scene Plan."
                    )
                existing_scenes = scene_blocks(approved["fields"].get("output", ""))
                duplicate = set(numbers) & set(existing_scenes)
                if duplicate:
                    raise ValueError(f"Scene {sorted(duplicate)} is duplicated or invalid.")
                if max(existing_group) < min(selected):
                    earlier_scenes.extend(existing_scenes)
                elif min(existing_group) > max(selected):
                    later_scenes.extend(existing_scenes)

            if earlier_scenes and numbers[0] != max(earlier_scenes) + 1:
                raise ValueError("Scene Plan must continue after the approved previous plan.")
            if later_scenes and numbers[-1] != min(later_scenes) - 1:
                raise ValueError("Scene Plan must continue before the approved next plan.")

        if stage in ("t5", "info"):
            result = output_json(fields["output"])
            if not result["scenes"]:
                raise ValueError("JSON must contain at least one Scene.")
            approved_plans = (self.plans(data, owner, chapters(fields.get("chapters", "")))
                              if stage == "t5" else self.plans(data, owner))
            expected = {n for _, text in approved_plans for n in scene_blocks(text)}
            numbers = [s.get("scene_number") if isinstance(s, dict) else None for s in result["scenes"]]
            if any(type(n) is not int for n in numbers) or len(set(numbers)) != len(numbers) or set(numbers) != expected:
                raise ValueError(f"JSON must contain exactly the planned Scene numbers {sorted(expected)}, with no duplicates.")
            for scene in result["scenes"]:
                if not isinstance(scene, dict) or type(scene.get("scene_number")) is not int:
                    raise ValueError("Each Scene needs an integer scene_number.")
                if stage == "info" and not isinstance(scene.get("summary"), str):
                    raise ValueError("Scene Info needs a text summary.")
                if stage == "t5" and (not isinstance(scene.get("prompt"), str) or not isinstance(scene.get("references"), list)):
                    raise ValueError("Generation JSON needs a prompt and a references array for each Scene.")

    def import_t5_chapters(self, project_id, stage_fields):
        """Import the entire approved T5 JSON into one child project for its Chapter group."""
        with self.store._lock:
            owner, _, data = self.read(project_id)
            key = self.key("t5", stage_fields)
            record = data.get(key, {})
            approved = record.get("approved")
            if not approved:
                raise ValueError("Duyệt kết quả T5 trước.")
            group_name = "+".join(map(str, chapters(approved["fields"].get("chapters", ""))))
            scenes = output_json(approved["fields"].get("output", ""))["scenes"]
        # The store lock is a non-reentrant threading.Lock. Release it before
        # calling the import method, which acquires that same lock itself.
        return self.store.import_scenes_to_chapter_group(
            owner, group_name, {"scenes": scenes})

    def save(self, project_id, stage, fields, revision, approve=False, source_hash="", prompt=""):
        if not isinstance(fields, dict) or any(not isinstance(v, str) for v in fields.values()):
            raise ValueError("Các ô nhập phải là văn bản.")
        if approve and stage in ("t5", "info"):
            fields = copy.deepcopy(fields)
            fields["output"] = json.dumps(output_json(fields.get("output", "")), ensure_ascii=False, indent=2)
        if len(json.dumps(fields)) + len(prompt) > 4_000_000:
            raise ValueError("Nội dung quá lớn (tối đa 4 MB mỗi lượt).")
        key = self.key(stage, fields)
        with self.store._lock, self.store._conn:
            self.store._conn.execute("BEGIN IMMEDIATE")
            owner, current, data = self.read(project_id)
            if revision != current:
                raise ValueError("Dữ liệu đã được sửa ở cửa sổ khác. Tải lại trước khi lưu.")
            record = data.setdefault(key, {"history": []})
            entry = {"fields": fields, "source_hash": source_hash, "prompt": prompt, "saved_at": time.time()}
            if approve:
                source = self.source(data, stage, fields, owner)
                if digest(source) != source_hash:
                    raise ValueError("Hãy tạo prompt từ input hiện tại trước khi duyệt kết quả.")
                self.validate_approval(stage, fields, data, key, owner)
                if record.get("approved"):
                    record["history"].append(copy.deepcopy(record["approved"]))
                record["approved"] = copy.deepcopy(entry)
            record["draft"] = entry
            self.store._conn.execute(
                "INSERT INTO prompt_workflows VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                (owner, current + 1, json.dumps(data, ensure_ascii=False)))
        return self.view(project_id)

    def view(self, project_id):
        with self.store._lock:
            owner, revision, data = self.read(project_id)
            statuses = {}
            for key, record in data.items():
                try:
                    self.require(data, key, owner)
                    statuses[key] = {"state": "approved", "message": "Đã duyệt"}
                except ValueError as exc:
                    statuses[key] = {"state": "stale" if record.get("approved") else "draft", "message": str(exc)}
            return {"project_id": owner, "revision": revision, "records": data, "statuses": statuses,
                    "assets": self.assets(owner),
                    "outline_chapters": outline_chapters(data.get("t2", {}).get("approved", {}).get("fields", {}).get("outline", ""))}


def register_prompt_routes(app, store, admin_auth):
    workflow = PromptWorkflow(store)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ImportValidationError as exc:
            raise HTTPException(exc.status_code, exc.report) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/admin/projects/{project_id}/prompt-workflow")
    def get_workflow(project_id: str, x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        return call(workflow.view, project_id)

    @app.post("/api/admin/projects/{project_id}/prompt-workflow/{stage}/generate")
    def generate(project_id: str, stage: str, body: dict = Body(...), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        fields = body.get("fields", {})
        if not isinstance(fields, dict) or any(not isinstance(v, str) for v in fields.values()):
            raise HTTPException(422, "Các ô nhập phải là văn bản.")
        return call(workflow.generate, project_id, stage, fields)

    @app.post("/api/admin/projects/{project_id}/prompt-workflow/t5/import-chapters")
    def import_t5_chapters(project_id: str, body: dict = Body(...), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        fields = body.get("fields", {})
        if not isinstance(fields, dict) or any(not isinstance(v, str) for v in fields.values()):
            raise HTTPException(422, "Các ô nhập phải là văn bản.")
        return call(workflow.import_t5_chapters, project_id, fields)

    @app.put("/api/admin/projects/{project_id}/prompt-workflow/{stage}")
    def save(project_id: str, stage: str, body: dict = Body(...), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        if not isinstance(body.get("prompt", ""), str) or not isinstance(body.get("source_hash", ""), str):
            raise HTTPException(422, "Prompt không hợp lệ.")
        return call(workflow.save, project_id, stage, body.get("fields", {}), body.get("revision"),
                    body.get("approve") is True, body.get("source_hash", ""), body.get("prompt", ""))

    @app.post("/api/admin/prompt-workflow/split")
    def split(body: dict = Body(...), x_admin_key: str | None = Header(default=None)):
        admin_auth(x_admin_key)
        if not isinstance(body.get("text"), str):
            raise HTTPException(422, "Kết quả AI phải là văn bản.")
        return {"fields": split_sections(body["text"])}

    return workflow

"""Offline workflow tests: persistence, source invalidation, grouping and actual templates."""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from prompt_workflow import PromptWorkflow, register_prompt_routes, split_sections
from store import TaskStore


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        self.store = TaskStore(self.db)
        self.project = self.store.create_project("Film")["id"]
        self.workflow = PromptWorkflow(self.store)

    def tearDown(self):
        self.store._conn.close()
        self.tmp.cleanup()

    def approve(self, stage, fields):
        generated = self.workflow.generate(self.project, stage, fields)
        return self.workflow.save(self.project, stage, fields, generated["revision"], True,
                                  generated["source_hash"], generated["prompt"])

    def story(self):
        return self.approve("t2", {"idea": "Chosen idea", "minutes": "20", "output": "AI",
            "story": "Whole film story", "outline": "CHAPTER 1 — Start\nCHAPTER 2 — End"})

    def plans(self):
        self.story()
        for n in (1, 2):
            self.approve("t3", {"chapters": str(n), "output": f"SCENE {n} — Scene {n}\nAction {n}"})

    def references(self):
        self.plans()
        self.approve("t4", {"output": "AI reference result", "reference_plan": "Hero and Castle",
            "reference_list": "Hero and Castle", "missing_decisions": "None",
            "reference_map": "SCENE 1 — Intro\nHero required\nSCENE 2 — Finale\nCastle required"})

    def test_actual_templates_and_scoped_prompt(self):
        prompt = self.workflow.generate(self.project, "t1", {"champion": "Garen", "minutes": "20"})["prompt"]
        self.assertIn("Garen", prompt)
        self.assertNotIn("[TÊN NHÂN VẬT]", prompt)
        self.references()
        result = self.workflow.generate(self.project, "t5", {"chapters": "1"})
        self.assertIn("Whole film story", result["prompt"])
        self.assertIn("Hero required", result["prompt"])
        self.assertIn("Castle required", result["prompt"])
        self.assertNotIn("Action 2", result["prompt"])
        self.assertIn("(No references available)", result["prompt"])
        self.assertNotIn("Riven — Two Years Later", result["prompt"])
        self.assertNotIn("[ĐIỀN STYLE CHUNG CỦA PHIM]", result["prompt"])
        self.assertIn("seedance-2.0", result["prompt"])
        info = self.workflow.generate(self.project, "info", {})
        self.assertLess(info["prompt"].index("Action 1"), info["prompt"].index("Action 2"))

    def test_update_replaces_group_preserves_history_and_marks_dependents_stale(self):
        self.references()
        self.approve("t3", {"chapters": "1", "output": "SCENE 1 — Changed\nNew action"})
        state = self.workflow.view(self.project)
        self.assertEqual(len(state["records"]["t3:1"]["history"]), 1)
        self.assertEqual(state["statuses"]["t4"]["state"], "stale")
        with self.assertRaisesRegex(ValueError, "nguồn đã thay đổi"):
            self.workflow.generate(self.project, "t5", {"chapters": "1"})

    def test_duplicate_scenes_overlap_and_incomplete_film_are_rejected(self):
        self.story()
        self.approve("t3", {"chapters": "1", "output": "SCENE 1 — Beginning"})
        with self.assertRaises(ValueError):
            self.approve("t3", {"chapters": "2", "output": "SCENE 1 — Duplicate"})
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.approve("t3", {"chapters": "1+2", "output": "SCENE 3 — Overlap"})
        with self.assertRaisesRegex(ValueError, "Chapter thiếu"):
            self.workflow.generate(self.project, "t4", {})
        with self.assertRaisesRegex(ValueError, "Chapter thiếu"):
            self.workflow.generate(self.project, "info", {})

    def test_drafts_do_not_replace_approved_and_reload_from_database(self):
        self.story()
        state = self.workflow.view(self.project)
        self.workflow.save(self.project, "t2", {"idea": "draft only"}, state["revision"])
        fresh = PromptWorkflow(self.store).view(self.project)
        self.assertEqual(fresh["records"]["t2"]["approved"]["fields"]["story"], "Whole film story")
        self.assertEqual(fresh["records"]["t2"]["draft"]["fields"]["idea"], "draft only")
        child = self.store.create_chapter(self.project, "Chapter 1")
        self.assertEqual(self.workflow.view(child["id"])["project_id"], self.project)
        other = self.store.create_project("Other")["id"]
        self.assertEqual(self.workflow.view(other)["records"], {})

    def test_optimistic_revision_and_output_source_check(self):
        generated = self.workflow.generate(self.project, "t1", {"champion": "A", "minutes": "20"})
        with self.assertRaisesRegex(ValueError, "input hiện tại"):
            self.workflow.save(self.project, "t1", {"champion": "B", "minutes": "20", "output": "AI"}, 0, True,
                               generated["source_hash"], generated["prompt"])
        self.story()
        with self.assertRaisesRegex(ValueError, "cửa sổ khác"):
            self.workflow.save(self.project, "t1", {}, 0)

    def test_json_scene_coverage(self):
        self.references()
        fields = {"chapters": "1", "output": json.dumps({"scenes": [{"scene_number": 2, "prompt": "Wrong", "references": []}]})}
        with self.assertRaisesRegex(ValueError, "exactly the planned Scene numbers"):
            self.approve("t5", fields)
        fields["output"] = json.dumps({"scenes": [{"scene_number": 1, "prompt": "Hero walks", "references": []}]})
        self.approve("t5", fields)

    def test_approved_t5_import_creates_chapter_and_imports_json_without_locking_up(self):
        self.references()
        output = {"scenes": [{"scene_number": 1, "scene_name": "Opening", "prompt": "Hero walks",
                               "model": "seedance-2.5", "ratio": "16:9", "duration": 30,
                               "start_end": False, "references": []}]}
        self.approve("t5", {"chapters": "1", "output": json.dumps(output)})

        result = self.workflow.import_t5_chapters(self.project, {"chapters": "1"})

        self.assertEqual(result["chapter_group"], "Chapter 1")
        self.assertEqual(result["imported_count"], 1)
        chapters = self.store.list_chapters(self.project)
        self.assertEqual([chapter["name"] for chapter in chapters], ["Chapter 1"])
        self.assertEqual(len(self.store.project_scene_details(chapters[0]["id"])), 1)

    def test_combined_groups_cannot_be_sliced_without_scene_chapter_mapping(self):
        self.story()
        self.approve("t3", {"chapters": "2+1", "output": "SCENE 1 — A\nSCENE 2 — B"})
        self.approve("t4", {"output": "AI", "reference_plan": "Hero", "reference_list": "Hero", "missing_decisions": "None", "reference_map": "SCENE 1 - Hero\nSCENE 2 - Hero"})
        with self.assertRaisesRegex(ValueError, "chọn đủ nhóm"):
            self.workflow.generate(self.project, "t5", {"chapters": "1"})
        self.workflow.generate(self.project, "t5", {"chapters": "1+2"})

    def test_split_markdown_and_authentication(self):
        fields = split_sections("## FULL STORY\nStory\n## FULL CHAPTER OUTLINE\nCHAPTER 1 — Start")
        self.assertEqual(fields["full_story"], "Story")
        actual = split_sections("## PART 1 — FULL STORY\nStory\n## PART 2 — STORY STRUCTURE\nStructure\n"
                                "## PART 3 — FULL CHAPTER OUTLINE\nCHAPTER 1 — Start\n## PART 4 — FILM OVERVIEW\nOverview")
        self.assertEqual(actual["full_story"], "Story")
        self.assertEqual(actual["full_chapter_outline"], "CHAPTER 1 — Start")
        refs = split_sections("CHARACTER REFERENCES\nHero\n## SCENE → REFERENCE MAP\nSCENE 1 — Hero\n"
                              "## FINAL MASTER REFERENCE LIST\nHero, Castle")
        self.assertEqual(refs["reference_map"], "SCENE 1 — Hero")
        self.assertEqual(refs["reference_plan"], "CHARACTER REFERENCES\nHero")
        app = FastAPI()
        def auth(key):
            if key != "test":
                raise HTTPException(401)
        register_prompt_routes(app, self.store, auth)
        with TestClient(app) as client:
            url = f"/api/admin/projects/{self.project}/prompt-workflow"
            self.assertEqual(client.get(url).status_code, 401)
            self.assertEqual(client.get(url, headers={"X-Admin-Key": "test"}).status_code, 200)
            self.assertEqual(client.post(url+"/t3/generate", json={"fields":{"chapters":"../"}},
                             headers={"X-Admin-Key":"test"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()

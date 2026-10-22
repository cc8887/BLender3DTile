"""Pure-Python regressions for benchmark evidence, with no Blender workers."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


SPEC = spec_from_file_location("b3dtile_benchmark_evaluate", Path(__file__).resolve().parents[1] / "benchmarks/evaluate.py")
evaluate = module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate)
REVISION = "0123456789abcdef"
OLD_REVISION = "fedcba9876543210"
GLB_ID = "a_123456789abc"
PNG_ID = "a_abcdef123456"


def call(number, *, session="one", completed=True, workers=1):
    return {"session_id": session, "event": "tool_call" if completed else "tool_call_started",
            "tool_call_number": number, "tool": "build_asset", "ok": True,
            "worker_runs": workers, "budget_admitted": True}


def test_worker_launches_sum_per_call_and_deduplicate_replay():
    calls = [call(1), call(2), call(3)]
    summary = evaluate._call_summary(calls + [dict(calls[1])])
    assert summary["worker_runs_reported"] == 3
    assert summary["worker_runs_by_session"] == {"one": 3}
    assert summary["completed"] == summary["build_attempts"] == 3
    assert summary["worker_runs_is_known_lower_bound"] is False


def test_incomplete_call_cannot_claim_zero_launches_or_double_count():
    audit = [call(1, completed=False, workers=0), call(1), call(2, completed=False, workers=0)]
    summary = evaluate._call_summary(audit)
    assert summary["worker_runs_reported"] == 1
    assert summary["worker_runs_is_known_lower_bound"] is True
    assert summary["incomplete_call_numbers"] == [2]
    assert summary["total_including_incomplete"] == 2
    assert summary["build_attempts"] == 2


def test_multiple_sessions_keep_separate_call_ids_and_sum_workers():
    summary = evaluate._call_summary([call(1, session="one"), call(1, session="two"), call(2, session="two")])
    assert summary["worker_runs_reported"] == 3
    assert summary["worker_runs_by_session"] == {"one": 1, "two": 2}
    assert summary["session_count"] == 2


def inspection(object_id="roof", revision=REVISION, ok=True):
    return {"tool": "inspect_object", "ok": ok, "tool_call_number": 5,
            "input": {"asset_id": "house_eval", "revision": revision, "object_id": object_id},
            "result_summary": {"revision": revision}}


OBSERVATION = {"objects": [{"id": "roof", "kit_id": "roof_gable_35_v1"},
                            {"id": "walls", "kit_id": "house_shell_2m_v1"},
                            {"id": "barrel", "kit_id": "barrel_12_v1"}]}


@pytest.mark.parametrize("calls", [[], [inspection(revision=OLD_REVISION)], [inspection(object_id="barrel")], [inspection(ok=False)]])
def test_missing_failed_stale_or_wrong_object_inspection_does_not_pass(calls):
    result = evaluate._inspection_evidence(calls, OBSERVATION, "house_eval", REVISION, "house")
    assert result["passed"] is False


@pytest.mark.parametrize("object_id", ["roof", "walls"])
def test_prompt_allows_either_roof_or_wall_inspection(object_id):
    result = evaluate._inspection_evidence([inspection(object_id=object_id)], OBSERVATION, "house_eval", REVISION, "house")
    assert result["passed"] is True
    assert result["successful_inspections"][0]["object_id"] == object_id


def test_zombie_inspection_requires_humanoid_and_resolved_revision():
    data = {"objects": [{"id": "hero", "kit_id": "humanoid_round_v1"}]}
    row = {"tool": "inspect_object", "ok": True, "input": {"asset_id": "zombie_eval", "revision": REVISION, "object_id": "hero"},
           "evidence": {"resolved_revision": REVISION}}
    assert evaluate._inspection_evidence([row], data, "zombie_eval", REVISION, "zombie")["passed"]
    row["evidence"]["resolved_revision"] = OLD_REVISION
    assert not evaluate._inspection_evidence([row], data, "zombie_eval", REVISION, "zombie")["passed"]


MANIFEST = {"revision": REVISION, "artifacts": {
    GLB_ID: {"path": "house_eval.glb", "mime_type": "model/gltf-binary"},
    PNG_ID: {"path": "contact_sheet.png", "mime_type": "image/png"}}}


@pytest.mark.parametrize("text", ["", f"成功，GLB artifact_id：`{GLB_ID}`。",
                                  f"最终 revision：`{REVISION}`。",
                                  f"最终 revision：`{OLD_REVISION}`；GLB：`{GLB_ID}`。",
                                  f"最终 revision：`{REVISION}`；GLB：`{PNG_ID}`。"])
def test_final_response_must_identify_current_revision_and_actual_glb(text):
    assert evaluate._final_response_evidence(text, MANIFEST, REVISION)["passed"] is False


def test_final_delivery_fields_accept_formatting_without_imposing_prose_rules():
    text = f"成功。最终 revision：`{REVISION}`；GLB artifact_id：`{GLB_ID}`。图片：`{PNG_ID}`。"
    result = evaluate._final_response_evidence(text, MANIFEST, REVISION)
    assert result["passed"] is True
    assert result["matching_glb_artifact_ids"] == [GLB_ID]


def test_report_status_derived_only_when_root_has_no_explicit_status():
    terminal = [{"status": "completed"}] * 7 + [{"status": "failed"}]
    assert evaluate._report_status({}, terminal) == "completed"
    assert evaluate._report_status({}, terminal[:-1] + [{"status": "running"}]) == "observing"
    assert evaluate._report_status({}, []) == "observing"
    assert evaluate._report_status({"status": "infrastructure_aborted"}, terminal) == "infrastructure_aborted"

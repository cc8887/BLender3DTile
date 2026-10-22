"""Offline evidence checks for one isolated, eight-run asset smoke experiment.

This script reads completed run artifacts. It neither invokes a model nor
changes a recipe/model. A passed gate is a technical observation, not an art
score or evidence of a model ranking. Usage is reported only when the CLI
actually emitted turn.completed.usage; wall time includes tools and rendering.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO
SEED = 20261022
DEFAULT_MODELS = {"luna": "gpt-6-luna", "terra": "gpt-5.6-terra", "sol": "gpt-6.1-sol", "astra": "gpt-6-astra"}
HOUSE_KITS = Counter({"house_shell_2m_v1": 1, "roof_gable_35_v1": 1, "door_roundtop_v1": 1,
                      "window_square_v1": 2, "barrel_12_v1": 1})
CHARACTER_TILES = {"char_face_sleepy", "char_skin_mint", "char_shirt_plum", "char_jeans_indigo"}


def load_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_jsonl(path: Path) -> tuple[list[dict], list[dict]]:
    records, errors = [], []
    if not path.is_file():
        return records, errors
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Record is not an object")
            records.append(value)
        except (ValueError, TypeError) as exc:
            errors.append({"file": str(path), "line": line_number, "error": str(exc)})
    return records, errors


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def contained_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaping artifact: {relative}")
    return path


def _gate(result: dict, name: str, passed: bool, **evidence) -> None:
    result["gates"][name] = {"passed": bool(passed), **evidence}


def _eligibility(status: str, root_config: dict) -> dict:
    if root_config.get("status") == "infrastructure_aborted":
        return {"eligible_for_comparison": False, "evaluation_status": "excluded_infrastructure",
                "exclusion_reason": root_config.get("reason", "Whole experiment aborted by infrastructure"),
                "failure_category": "infrastructure"}
    if status in ("missing", "pending", "running", "unknown"):
        return {"eligible_for_comparison": False, "evaluation_status": "not_evaluated",
                "exclusion_reason": f"Trial is {status}; no completed observation", "failure_category": None}
    if status in ("timeout", "timed_out", "cancelled", "canceled", "infrastructure_aborted") or status.startswith("infrastructure_"):
        return {"eligible_for_comparison": False, "evaluation_status": "not_evaluated_infrastructure",
                "exclusion_reason": f"Trial was interrupted: {status}; not attributed to asset quality",
                "failure_category": "infrastructure"}
    return {"eligible_for_comparison": True, "evaluation_status": "execution_failed" if status == "failed" else "evaluated",
            "exclusion_reason": None, "failure_category": "execution" if status == "failed" else None}


def _objects(observation: dict) -> list[dict]:
    value = observation.get("objects", [])
    return [{"id": key, **record} for key, record in value.items()] if isinstance(value, dict) else value


def _call_summary(audit: list[dict]) -> dict:
    calls = [row for row in audit if row.get("event") == "tool_call"]
    started = [row for row in audit if row.get("event") == "tool_call_started"]
    completed_ids = {(row.get("session_id"), row.get("tool_call_number")) for row in calls}
    unique_calls = {(row.get("session_id"), row.get("tool_call_number")): row for row in started + calls}
    sessions = {row.get("session_id") for row in audit if row.get("session_id")}
    worker_counts = {session: max((row.get("worker_runs", 0) for row in audit
                                  if row.get("session_id") == session and isinstance(row.get("worker_runs", 0), (int, float))), default=0)
                     for session in sessions}
    incomplete = [row.get("tool_call_number") for row in started
                  if (row.get("session_id"), row.get("tool_call_number")) not in completed_ids]
    failures = [{"tool": row.get("tool"), "tool_call_number": row.get("tool_call_number"),
                 "error": row.get("error"), "budget_admitted": row.get("budget_admitted")}
                for row in calls if row.get("ok") is not True]
    return {"completed": len(calls), "total_including_incomplete": len(unique_calls),
            "by_tool": dict(Counter(row.get("tool", "unknown") for row in calls)),
            "failed": len(failures), "errors": failures, "incomplete_call_numbers": incomplete,
            "session_ids": sorted(sessions), "session_count": len(sessions),
            "build_attempts": sum(row.get("tool") == "build_asset" and row.get("budget_admitted") is True for row in unique_calls.values()),
            "worker_runs_reported": sum(worker_counts.values()), "worker_runs_by_session": worker_counts,
            "tool_duration_seconds_sum": sum(row.get("duration_seconds", 0) for row in calls if isinstance(row.get("duration_seconds"), (int, float)))}


def _usage_and_events(events: list[dict]) -> tuple[dict | str, list[dict], list[str], list[dict]]:
    usages, errors, model_ids, warnings = [], [], [], []
    for event in events:
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usages.append(event["usage"])
        is_error = event.get("type") in ("error", "turn.failed") or (
            isinstance(event.get("item"), dict) and event["item"].get("type") == "error")
        if is_error:
            # Codex emits this optional-feature notice as item.type=error even
            # when the turn and tools run normally. Preserve it as a warning.
            message = str(event.get("item", {}).get("message", event.get("message", "")))
            (warnings if "Under-development features enabled:" in message else errors).append(event)
        # Only explicit CLI metadata counts as confirmation. Never infer model
        # identity from prose, timestamps, error text, token counts or filenames.
        for source in (event, event.get("session", {}), event.get("thread", {})):
            if isinstance(source, dict):
                for key in ("model", "resolved_model", "model_id"):
                    value = source.get(key)
                    if isinstance(value, str) and value not in model_ids:
                        model_ids.append(value)
    if not usages:
        return "unknown", errors, model_ids, warnings
    totals = {}
    for usage in usages:
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return {"source": "CLI turn.completed.usage", "turns": len(usages), "totals": totals, "raw": usages}, errors, model_ids, warnings


def _revision_discipline(calls: list[dict], asset_id: str) -> dict:
    builds = [row for row in calls if row.get("tool") == "build_asset"
              and row.get("input", {}).get("recipe", {}).get("asset_id") == asset_id]
    current = None
    violations = []
    successful_revisions = []
    request_payloads = {}
    for index, call in enumerate(builds):
        arguments = call.get("input", {})
        request_id = arguments.get("request_id")
        payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        if request_id in request_payloads and request_payloads[request_id] != payload:
            violations.append({"call": call.get("tool_call_number"), "reason": "request_id_reused_for_different_input"})
        request_payloads[request_id] = payload
        if "expected_revision" not in arguments or arguments["expected_revision"] != current:
            violations.append({"call": call.get("tool_call_number"), "reason": "first_build_requires_null" if index == 0 else "revision_guard_mismatch",
                               "expected": current, "actual": arguments.get("expected_revision", "missing")})
        summary = call.get("result_summary") or {}
        if call.get("ok") is True and isinstance(summary.get("revision"), str):
            # A cached older request is not a new publication. Its is_latest
            # flag, when present, prevents rewinding our observed revision.
            if summary.get("is_latest", True):
                current = summary["revision"]
            successful_revisions.append(summary["revision"])
    return {"passed": bool(builds) and not violations, "build_calls": len(builds),
            "violations": violations, "successful_revisions": successful_revisions,
            "last_successful_revision": current}


def _official_check(glb_path: Path, saved: dict) -> dict:
    script = BACKEND / "tools" / "validate_glb.cjs"
    package = BACKEND / ".tools" / "gltf-validator" / "node_modules" / "gltf-validator"
    node = shutil.which("node")
    if not node or not script.is_file() or not package.is_dir():
        return {"source": "saved_hashed_validation_report", "fresh_check": "unavailable", **saved}
    try:
        process = subprocess.run([node, str(script), str(glb_path)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=120, check=False)
        if process.returncode:
            return {"source": "fresh_offline_check", "ok": False, "status": "failed", "error": process.stderr[-2000:]}
        report = json.loads(process.stdout)
        issues = report["issues"]
        return {"source": "fresh_offline_check", "status": "completed", "ok": issues["numErrors"] == 0,
                "errors": issues["numErrors"], "warnings": issues["numWarnings"],
                "messages": issues.get("messages", []), "validator_version": report.get("validatorVersion")}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return {"source": "fresh_offline_check", "ok": False, "status": "failed", "error": str(exc)}


def _paint_evidence(catalog: dict, recipe: dict, directory: Path) -> dict:
    from PIL import Image, ImageColor
    from b3dtile.atlas import build_atlases

    paints = [op for op in recipe.get("ops", []) if op.get("op") == "paint_tile"]
    tiles = {tile["id"]: tile for tile in catalog["tiles"]}
    evidence = []
    with tempfile.TemporaryDirectory(prefix="b3dtile_benchmark_atlas_") as temporary:
        temp = Path(temporary)
        defaults = build_atlases(catalog, {"seed": SEED, "ops": []}, temp / "defaults")
        no_marks = build_atlases(catalog, {"seed": SEED, "ops": [{k: v for k, v in op.items() if k != "marks"} for op in paints]}, temp / "without_marks")
        for op in paints:
            tile = tiles[op["tile_id"]]
            x, y, width, height = tile["rect_px"]
            pad = tile["padding_px"]
            path = contained_file(directory, f"textures/{tile['atlas_id']}.png")
            with Image.open(path) as source, Image.open(defaults[tile["atlas_id"]]) as base, Image.open(no_marks[tile["atlas_id"]]) as pattern:
                current, initial, unmarked = source.convert("RGB"), base.convert("RGB"), pattern.convert("RGB")
                # Only compare content; regenerated border pixels do not inflate
                # either the unique-mark count or the count of changed texels.
                rectangle = (x + pad, y + pad, x + width - pad, y + height - pad)
                changes = sum(current.getpixel((px, py)) != initial.getpixel((px, py))
                              for py in range(rectangle[1], rectangle[3]) for px in range(rectangle[0], rectangle[2]))
                marks = {(mark["x"], mark["y"]): mark["color_index"] for mark in op.get("marks", [])}
                custom_changes = mismatch = default_changes = 0
                colors = [ImageColor.getrgb(color) for color in catalog["palettes"][op["palette"]]]
                for (mx, my), index in marks.items():
                    if not (type(mx) is int and type(my) is int and 0 <= mx < width - 2*pad and 0 <= my < height - 2*pad and type(index) is int and 0 <= index < 16):
                        mismatch += 1
                        continue
                    point = (x + pad + mx, y + pad + my)
                    actual = current.getpixel(point)
                    mismatch += actual != colors[index]
                    custom_changes += actual != unmarked.getpixel(point)
                    default_changes += actual != initial.getpixel(point)
                evidence.append({"tile_id": tile["id"], "atlas_id": tile["atlas_id"], "texture_path": str(path),
                                 "pattern": op["pattern"], "palette": op["palette"], "distinct_mark_coordinates": len(marks),
                                 "content_pixels_changed_from_default": changes,
                                 "marked_pixels_changed_from_default": default_changes,
                                 "marked_pixels_changed_from_selected_pattern": custom_changes,
                                 "marked_pixel_mismatches": mismatch,
                                 "custom_marks_gate": len(marks) >= 16 and changes > 0 and custom_changes > 0 and mismatch == 0})
    return {"tiles": evidence, "unique_painted_tiles": len({op["tile_id"] for op in paints}),
            "custom_marks_gate": any(item["custom_marks_gate"] for item in evidence)}


def _preview_evidence(calls: list[dict], manifest: dict, observation: dict, revision: str, task: str) -> dict:
    artifacts = manifest.get("artifacts", {})
    image_by_path = {image["path"]: image for image in observation.get("images", [])}
    read_images, invalid = [], []
    for call in calls:
        if call.get("tool") != "read_preview" or call.get("ok") is not True:
            continue
        arguments, evidence = call.get("input", {}), call.get("evidence", {})
        if arguments.get("asset_id") != manifest["asset_id"] or arguments.get("revision") != revision:
            continue
        artifact = artifacts.get(arguments.get("artifact_id"), {})
        image = image_by_path.get(artifact.get("path"))
        if (image is None or artifact.get("mime_type") != "image/png" or evidence.get("revision") != revision
            or evidence.get("artifact_id") != arguments.get("artifact_id") or evidence.get("sha256") != artifact.get("sha256")
            or call.get("result_summary", {}).get("native_image") is not True):
            invalid.append(call.get("tool_call_number"))
            continue
        read_images.append({"artifact_id": arguments["artifact_id"], "call": call.get("tool_call_number"), **image})
    def read(predicate):
        return any(predicate(image) for image in read_images)
    contact = read(lambda image: image.get("path") == observation.get("contact_sheet"))
    uv = read(lambda image: image.get("mode") == "uv_debug")
    checks = {"contact_sheet_read": contact, "uv_debug_read": uv}
    if task == "house":
        checks["three_quarter_beauty_read"] = read(lambda image: image.get("mode") == "beauty" and image.get("camera") == "three_quarter" and image.get("pose") == "rest")
    else:
        checks["arms_120_beauty_read"] = read(lambda image: image.get("mode") == "beauty" and image.get("pose") == "arms_raise_120")
        elbow = read(lambda image: image.get("mode") == "beauty" and image.get("pose") == "elbows_bend_90")
        elbow_panels = []
        for image in read_images:
            if image.get("mode") == "contact_sheet":
                for panel in image.get("panels", []):
                    target = image_by_path.get(panel.get("image"), {})
                    if target.get("mode") == "beauty" and target.get("pose") == "elbows_bend_90":
                        elbow_panels.append(panel["image"])
        checks["elbows_90_seen_directly_or_in_sheet"] = elbow or bool(elbow_panels)
    observed = any(call.get("tool") == "observe_asset" and call.get("ok") is True
                   and call.get("input", {}).get("asset_id") == manifest["asset_id"]
                   and ((call.get("result_summary") or {}).get("revision") == revision
                        or call.get("evidence", {}).get("resolved_revision") == revision) for call in calls)
    checks["final_revision_observed"] = observed
    return {"passed": all(checks.values()) and not invalid, "checks": checks,
            "read_images": [{key: image.get(key) for key in ("artifact_id", "call", "path", "mode", "camera", "pose", "sha256")} for image in read_images],
            "invalid_evidence_calls": invalid}


def evaluate_run(run_dir: Path, alias: str, task: str, configured_model: str, root_config: dict) -> dict:
    result = {"run": run_dir.name, "alias": alias, "task": task, "run_directory": str(run_dir),
              "requested_model": configured_model, "configured_model": "unknown", "confirmed_model": "unknown",
              "status": "missing", "gates": {}, "evaluation_errors": [], **_eligibility("missing", root_config)}
    try:
        metadata = load_json(run_dir / "run.json", {})
        result.update(status=metadata.get("status", "missing" if not run_dir.exists() else "unknown"),
                      exit_code=metadata.get("exit_code"), elapsed_seconds=metadata.get("elapsed_seconds", "unknown"),
                      effort=metadata.get("effort", root_config.get("effort", "unknown")),
                      configured_model=metadata.get("model", configured_model), run_metadata=metadata)
        result.update(_eligibility(result["status"], root_config))
        audit, audit_errors = load_jsonl(run_dir / "audit.jsonl")
        events, event_errors = load_jsonl(run_dir / "events.jsonl")
        calls = [row for row in audit if row.get("event") == "tool_call"]
        result["tool_calls"] = _call_summary(audit)
        counts = result["tool_calls"]
        deviations = []
        if counts["session_count"] > 1:
            deviations.append("MCP server restarted or multiple sessions used; per-process budgets are not a shared quota")
        if counts["total_including_incomplete"] > root_config.get("max_tool_calls", 40):
            deviations.append("Total tools/call count exceeds trial quota")
        if counts["build_attempts"] > root_config.get("max_build_calls", 3):
            deviations.append("Total admitted build calls across sessions exceed trial quota")
        result["protocol_deviations"] = deviations
        _gate(result, "trial_budget_and_isolation", not deviations, session_count=counts["session_count"],
              tool_calls=counts["total_including_incomplete"], admitted_build_calls=counts["build_attempts"], deviations=deviations)
        result["usage"], result["cli_errors"], event_models, result["cli_warnings"] = _usage_and_events(events)
        result["model_evidence"] = {"run_json_model": metadata.get("model"), "CLI_event_models": event_models}
        confirmed = metadata.get("resolved_model", metadata.get("actual_model"))
        if not confirmed and event_models:
            confirmed = event_models[-1]
        result["confirmed_model"] = confirmed or "unknown"
        result["evaluation_errors"].extend(audit_errors + event_errors)
        result["paths"] = {name: str(run_dir / name) for name in ("audit.jsonl", "events.jsonl", "stderr.log", "final.md") if (run_dir / name).is_file()}
        _gate(result, "CLI_completed", metadata.get("exit_code") == 0 and metadata.get("status") not in ("running", "pending", "timeout", "failed", "missing"), status=metadata.get("status"), exit_code=metadata.get("exit_code"))
        _gate(result, "audit_available", bool(calls) and not audit_errors and not result["tool_calls"]["incomplete_call_numbers"], completed_calls=len(calls), parse_errors=audit_errors)
        _gate(result, "catalog_read", any(call.get("tool") == "get_catalog" and call.get("ok") is True for call in calls))
        asset_id = task + "_eval"
        result["asset_id"] = asset_id
        result["revision_discipline"] = _revision_discipline(calls, asset_id)
        _gate(result, "revision_discipline", result["revision_discipline"]["passed"], **{k: v for k, v in result["revision_discipline"].items() if k != "passed"})
        asset_dir = run_dir / "assets" / asset_id
        latest = load_json(asset_dir / "latest.json", {})
        revision = latest.get("revision")
        result["revision"] = revision
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{16}", revision) is None:
            _gate(result, "published_asset", False, reason="No valid latest success pointer")
            return _finish(result)
        directory = (asset_dir / "revisions" / revision).resolve()
        manifest_path = contained_file(directory, "manifest.json")
        manifest = load_json(manifest_path)
        manifest_hash_ok = latest.get("manifest_sha256") == sha256(manifest_path)
        integrity = []
        for artifact_id, artifact in manifest.get("artifacts", {}).items():
            try:
                path = contained_file(directory, artifact["path"])
                if sha256(path) != artifact["sha256"]:
                    integrity.append({"artifact_id": artifact_id, "path": artifact["path"], "error": "hash_mismatch"})
            except (OSError, ValueError, KeyError) as exc:
                integrity.append({"artifact_id": artifact_id, "error": str(exc)})
        _gate(result, "published_asset", manifest_hash_ok and not integrity and manifest.get("revision") == revision and manifest.get("asset_id") == asset_id,
              manifest_hash_ok=manifest_hash_ok, artifact_errors=integrity, artifacts=len(manifest.get("artifacts", {})))
        request = load_json(contained_file(directory, "request.json"))
        recipe = request["params"]
        observation = load_json(contained_file(directory, "observation.json"))
        validation = load_json(contained_file(directory, "validation.json"))
        model_data = load_json(contained_file(directory, "model_data.json"))
        objects = _objects(observation)
        ops = recipe.get("ops", [])
        kits = Counter(obj["kit_id"] for obj in objects)
        result.update(component_count=len(objects), components=dict(kits), vertices=sum(obj.get("vertices", 0) for obj in objects),
                      triangles=sum(obj.get("triangles", 0) for obj in objects), bones=sum(obj.get("bone_count", 0) for obj in objects),
                      artifact_directory=str(directory), build_implementation_sha256=manifest.get("implementation_sha256"),
                      build_catalog_sha256=manifest.get("catalog_sha256"))
        _gate(result, "seed_and_snapshot", recipe.get("seed") == SEED and observation.get("revision") == revision and observation.get("asset_id") == asset_id,
              seed=recipe.get("seed"), required_seed=SEED)
        if task == "house":
            _gate(result, "composition", kits == HOUSE_KITS and len(objects) == 6, actual=dict(kits), expected=dict(HOUSE_KITS))
            attaches = [op for op in ops if op.get("op") == "attach"]
            placed = {op["id"]: op["kit_id"] for op in ops if op.get("op") == "place"}
            expected = Counter({("roof_gable_35_v1", "base", "house_shell_2m_v1", "roof_mount"): 1,
                                ("door_roundtop_v1", "mount", "house_shell_2m_v1", "door_mount"): 1,
                                ("window_square_v1", "mount", "house_shell_2m_v1", "window_left"): 1,
                                ("window_square_v1", "mount", "house_shell_2m_v1", "window_right"): 1})
            actual = Counter((placed.get(op["child"]), op["child_socket"], placed.get(op["parent"]), op["parent_socket"]) for op in attaches)
            attached_metadata = all(model_data.get(op["child"], {}).get("meta", {}).get("attachment") == {
                "parent": op["parent"], "parent_socket": op["parent_socket"], "child_socket": op["child_socket"]} for op in attaches)
            _gate(result, "certified_attachments", actual == expected and len(attaches) == 4 and attached_metadata, count=len(attaches), metadata_matches=attached_metadata)
        else:
            _gate(result, "composition", kits == Counter({"humanoid_round_v1": 1}), actual=dict(kits), expected={"humanoid_round_v1": 1})
            bindings = [op for op in ops if op.get("op") == "bind_humanoid"]
            rigs = validation.get("rig", {})
            _gate(result, "rig_and_animation", result["bones"] == 20 and len(bindings) == 1 and bindings[0].get("preset") == "humanoid_20_v1"
                  and bool(rigs) and all(item.get("ok") is True and item.get("metrics", {}).get("bones") == 20 for item in rigs.values())
                  and ops[-1].get("clips") == "rig_smoke_v1", bones=result["bones"], bindings=bindings, clips=ops[-1].get("clips"))
        from b3dtile.contracts import load_catalog
        from b3dtile.glb import inspect_glb
        catalog = load_catalog()
        result["painting"] = _paint_evidence(catalog, recipe, directory)
        painted_tiles = {item["tile_id"] for item in result["painting"]["tiles"]}
        paint_count_ok = len({tile for tile in painted_tiles if tile.startswith("env_")}) >= 3 if task == "house" else CHARACTER_TILES <= painted_tiles
        _gate(result, "custom_pixel_textures", paint_count_ok and result["painting"]["custom_marks_gate"], painted_tiles=sorted(painted_tiles),
              custom_marks_gate=result["painting"]["custom_marks_gate"], criterion="At least 16 distinct coordinates, matching actual pixels, with a real change from both default tile and selected unmarked pattern")
        reports_ok = (validation.get("geometry", {}).get("ok") is True and bool(validation.get("uv"))
                      and all(item.get("ok") is True for item in validation.get("uv", {}).values())
                      and all(item.get("ok") is True for item in validation.get("rig", {}).values()))
        _gate(result, "geometry_uv_rig_validation", reports_ok, geometry=validation.get("geometry", {}).get("ok"),
              uv={key: item.get("ok") for key, item in validation.get("uv", {}).items()}, rig={key: item.get("ok") for key, item in validation.get("rig", {}).items()})
        glb_path = contained_file(directory, observation.get("glb", asset_id + ".glb"))
        fresh_glb = inspect_glb(glb_path, require_skin=task == "zombie", require_animation=task == "zombie")
        result["glb"] = {"path": str(glb_path), "saved": validation.get("glb", {}), "fresh": fresh_glb}
        _gate(result, "GLB_binary_validation", validation.get("glb", {}).get("ok") is True and fresh_glb.get("ok") is True, fresh_errors=fresh_glb.get("errors", []))
        saved_official = validation.get("khronos", {})
        official = _official_check(glb_path, saved_official)
        result["official_validator"] = {"saved": saved_official, "evaluation": official}
        _gate(result, "official_zero_errors", saved_official.get("ok") is True and saved_official.get("errors") == 0
              and official.get("ok") is True and official.get("errors") == 0, source=official.get("source"), errors=official.get("errors"), warnings=official.get("warnings"))
        result["previews"] = _preview_evidence(calls, manifest, observation, revision, task)
        for image in result["previews"]["read_images"]:
            image["absolute_path"] = str(contained_file(directory, image["path"]))
        result["contact_sheet_path"] = str(contained_file(directory, observation["contact_sheet"]))
        _gate(result, "final_revision_images_read", result["previews"]["passed"], **result["previews"]["checks"])
        final_text = (run_dir / "final.md").read_text(encoding="utf-8", errors="replace") if (run_dir / "final.md").is_file() else ""
        result["final_response_mentions_revision"] = revision in final_text
        return _finish(result)
    except (OSError, ValueError, KeyError, TypeError, IndexError, ImportError) as exc:
        result["evaluation_errors"].append({"type": type(exc).__name__, "message": str(exc)})
        _gate(result, "evaluation_complete", False, reason=str(exc))
        return _finish(result)


def _finish(result: dict) -> dict:
    result["evidence_gates_passed"] = bool(result["gates"]) and all(gate["passed"] for gate in result["gates"].values()) and not result["evaluation_errors"]
    result["machine_gates_passed"] = result["eligible_for_comparison"] and result["evidence_gates_passed"]
    result["failed_gates"] = [name for name, gate in result["gates"].items() if not gate["passed"]]
    if not result["eligible_for_comparison"]:
        result["conclusion"] = result["evaluation_status"]
    elif result["evaluation_status"] == "execution_failed":
        result["conclusion"] = "CLI_or_model_execution_failed_not_an_art_score"
    else:
        result["conclusion"] = "technical_smoke_pass" if result["machine_gates_passed"] else "not_all_technical_gates_met"
        if not result["machine_gates_passed"]:
            result["failure_category"] = "technical_contract"
    return result


def _markdown(report: dict) -> str:
    def safe(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    def link(path, title=None):
        return f"[{title or Path(path).name}](<{Path(path).as_posix()}>)"
    lines = ["# 独立资产构建冒烟记录", "", "这是一次固定任务、固定种子的技术冒烟；不评分主观美术，不据此比较模型排名。", "",
             f"基线提交：`{report.get('baseline_commit', 'unknown')}`；effort：`{report.get('effort', 'unknown')}`；seed：`{SEED}`。", "",
             "耗时是进程墙钟时间，包含模型等待、工具、Blender 构建和渲染，不是纯推理耗时。Token 仅来自 CLI 实际输出的 `turn.completed.usage`；缺失即 unknown。", "",
             "模型列是 requested_model（请求的模型 ID）；仅当事件或运行元数据提供明确 resolved/actual 字段时，另列已确认 ID，不推测后端身份。", "",
             "| Run | requested_model | 已确认 ID | 运行状态/退出码 | 观察状态 | 工具调用/失败 | Token input/output | 墙钟秒 | 组件/骨骼 |",
             "|---|---|---|---|---|---|---|---|---|"]
    if report.get("status") == "infrastructure_aborted":
        lines[2:2] = ["**整轮因基础设施问题中止：8 个计划试验全部排除，包括未启动项；不纳入模型比较，也不判定模型失败。**", "",
                      "原因：" + str(report.get("infrastructure_reason", "unknown")), ""]
    for run in report["runs"]:
        usage = run.get("usage", "unknown")
        totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
        tokens = f"{totals.get('input_tokens', 'unknown')}/{totals.get('output_tokens', 'unknown')}"
        elapsed = run.get("elapsed_seconds", "unknown")
        if isinstance(elapsed, (int, float)):
            elapsed = f"{elapsed:.1f}"
        calls = run.get("tool_calls", {})
        if not run["eligible_for_comparison"]:
            observation_status = "已排除：基础设施中止" if run["evaluation_status"] == "excluded_infrastructure" else "未评估"
        elif run["evaluation_status"] == "execution_failed":
            observation_status = "CLI/模型执行失败；不评价美术"
        else:
            observation_status = "技术门槛通过" if run["machine_gates_passed"] else "技术合同未满足；不评价美术"
        row = [run["run"], run.get("requested_model", "unknown"), run.get("confirmed_model", "unknown"),
               f"{run['status']}/{run.get('exit_code', 'unknown')}", observation_status,
               f"{calls.get('completed', 0)}/{calls.get('failed', 0)}", tokens, elapsed,
               f"{run.get('component_count', 'unknown')}/{run.get('bones', 'unknown')}"]
        lines.append("| " + " | ".join(safe(v) for v in row) + " |")
    for run in report["runs"]:
        lines.extend(["", f"## {run['run']}", "", f"Revision：`{run.get('revision', 'unknown')}`。"])
        if not run["eligible_for_comparison"]:
            lines.extend(["", "不纳入模型比较；" + str(run.get("exclusion_reason", "未完成评估")) + "。原始日志仅保留为诊断证据。"])
        elif run.get("failed_gates"):
            lines.extend(["", "未满足：" + "、".join(f"`{name}`" for name in run["failed_gates"]) + "。"])
        if run.get("protocol_deviations"):
            lines.extend(["", "试验协议偏离：" + "；".join(run["protocol_deviations"]) + "。"])
        if run.get("glb", {}).get("path"):
            lines.extend(["", f"GLB：{link(run['glb']['path'])}；总览：{link(run['contact_sheet_path'])}。"])
        if run.get("previews", {}).get("read_images"):
            lines.extend(["", "已读取最终版本图片：", ""])
            for image in run["previews"]["read_images"]:
                label = f"{image.get('mode')}/{image.get('camera')}/{image.get('pose')}"
                lines.append(f"- {link(image['absolute_path'], label)}")
        if run.get("painting", {}).get("tiles"):
            lines.extend(["", "贴图证据（tile：不同 marks 坐标数 / 实际自定义变色像素数）：", ""])
            for tile in run["painting"]["tiles"]:
                lines.append(f"- `{tile['tile_id']}`：{tile['distinct_mark_coordinates']} / {tile['marked_pixels_changed_from_selected_pattern']}。")
        failures = run.get("tool_calls", {}).get("errors", [])
        if failures:
            lines.extend(["", "工具错误：", ""])
            for failure in failures[:12]:
                message = json.dumps(failure.get("error"), ensure_ascii=False)
                lines.append(f"- 调用 {failure.get('tool_call_number')} `{failure.get('tool')}`：{safe(message[:500])}")
        if run.get("cli_errors") or run.get("evaluation_errors"):
            lines.extend(["", "CLI / 评估诊断：", ""])
            for error in (run.get("cli_errors", []) + run.get("evaluation_errors", []))[:8]:
                lines.append("- " + safe(json.dumps(error, ensure_ascii=False)[:800]))
        if run.get("cli_warnings"):
            lines.extend(["", f"CLI 配置提示 {len(run['cli_warnings'])} 条，单独记录，不计为模型或工具失败。"])
        if run.get("paths"):
            lines.extend(["", "原始记录：" + "、".join(link(path) for path in run["paths"].values()) + "。"])
    lines.extend(["", "## 解释边界", ""])
    lines.extend("- " + item for item in report["limitations"])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    global BACKEND
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    run_root = args.run_root.resolve()
    config = load_json(run_root / "run.json", {})
    BACKEND = run_root / "backend" if (run_root / "backend" / "src" / "b3dtile").is_dir() else REPO
    sys.path.insert(0, str(BACKEND / "src"))
    fingerprint_errors = []
    for relative, expected_hash in config.get("backend_source_fingerprints", {}).items():
        path = BACKEND / relative
        if not path.is_file() or sha256(path) != expected_hash:
            fingerprint_errors.append(relative)
    models = config.get("models", DEFAULT_MODELS)
    if not isinstance(models, dict):
        raise ValueError("Root run.json models must map aliases to model IDs")
    aliases = list(DEFAULT_MODELS)
    report = {"evaluation_version": "1.0", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "run_root": str(run_root), "baseline_commit": config.get("baseline_commit", "unknown"),
              "status": config.get("status", "observing"), "infrastructure_reason": config.get("reason"),
              "effort": config.get("effort", "unknown"), "seed": SEED, "root_run_metadata": config,
              "evaluator_backend": str(BACKEND), "backend_fingerprint_mismatches": fingerprint_errors,
              "experiment": "single_smoke_no_ranking", "runs": [],
              "limitations": [
                  "每个模型每个任务仅一次隔离运行；这不能证明模型质量、稳定性或成本排名。",
                  "可爱程度、构图、装饰审美与读图理解不由这些机器门槛评分。read_preview 证据证明图片被工具返回，不证明模型理解正确。",
                  "计时包含工具和渲染等待；工具调用时长相加可能与并发运行重叠，不能从总耗时直接减出纯推理耗时。",
                  "并发运行共享宿主机资源与未知的服务端缓存状态；本报告不做耗时、成本或 token 效率排名。",
                  "没有 CLI usage 时不估算 token；配置模型 ID 不自动等同于服务端确认的底层模型版本。",
                  "贴图证据要求至少 16 个不同标记坐标，且存在真实自定义像素变化；不声称每一个标记都改变了颜色。",
                  "验证针对当前固定 kit、atlas、骨架和 GLB 合同；不涵盖自由建模能力或目标 UE/Godot 运行时验收。",
                  "官方验证若不能在评估机重跑，只引用已纳入哈希验证的构建报告，并明确标注来源。",
              ]}
    for alias in aliases:
        for task in ("house", "zombie"):
            configured = models.get(alias, DEFAULT_MODELS[alias])
            result = evaluate_run(run_root / "runs" / f"{alias}_{task}", alias, task, configured, config)
            if config.get("backend_source_fingerprints"):
                _gate(result, "frozen_backend_integrity", not fingerprint_errors, mismatched_files=fingerprint_errors)
                _finish(result)
            report["runs"].append(result)
    report["counts"] = {"runs": len(report["runs"]),
                        "observed_trials": sum(run["eligible_for_comparison"] for run in report["runs"]),
                        "excluded_trials": sum(not run["eligible_for_comparison"] for run in report["runs"]),
                        "execution_failures": sum(run["eligible_for_comparison"] and run["evaluation_status"] == "execution_failed" for run in report["runs"]),
                        "technical_passes": sum(run["machine_gates_passed"] for run in report["runs"]),
                        "published_assets": sum(run["gates"].get("published_asset", {}).get("passed", False) for run in report["runs"])}
    json_path = (args.json_output or run_root / "evaluation.json").resolve()
    markdown_path = (args.markdown_output or run_root / "report.md").resolve()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path), "counts": report["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

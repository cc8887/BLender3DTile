"""Process-isolated builds, immutable revisions and freshness-aware observations."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .contracts import (ContractError, canonical_bytes, catalog_digest, file_sha256,
                        implementation_digest, load_catalog, load_schema, parse_json, validate_request)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path):
    return parse_json(path.read_text(encoding="utf-8"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,47}", value) is None:
        raise ContractError("E_ID", "Invalid asset or request ID")
    return value


@contextmanager
def asset_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".build.lock"
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ContractError("E_BUILD_BUSY", "Asset has an active build; retry after it finishes") from exc
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "started": now()}))
        yield
    finally:
        path.unlink(missing_ok=True)


class AssetService:
    def __init__(self, output_root: str | Path = "out", *, backend: str = "bpy",
                 blender_executable: str | None = None, timeout: int = 900):
        self.root = Path(output_root).resolve()
        self.backend = backend
        self.blender_executable = blender_executable
        self.timeout = timeout

    def get_catalog(self) -> dict:
        return {"catalog": load_catalog(), "catalog_sha256": catalog_digest(), "schema": load_schema(),
                "observation_workflow": ["build_asset", "observe_asset", "read_preview", "build_asset with expected_revision"],
                "runtime": {"python": "3.11", "bpy": "4.5.14"}}

    def execute(self, request: dict) -> dict:
        try:
            validate_request(request)
            method = request["method"]
            if method == "get_catalog":
                return {"ok": True, "request_id": request["request_id"], "result": self.get_catalog()}
            if method == "build_asset":
                return self.build_asset(request)
            if method == "observe_asset":
                return {"ok": True, "result": self.observe_asset(**request["params"])}
            if method == "inspect_object":
                return {"ok": True, "result": self.inspect_object(**request["params"])}
            if method == "read_preview":
                path = self.artifact_path(**request["params"], images_only=True)
                return {"ok": True, "result": {"path": str(path), "sha256": file_sha256(path), "mime_type": "image/png", "revision": request["params"]["revision"]}}
            raise ContractError("E_METHOD", "Unsupported method")
        except ContractError as exc:
            return {"ok": False, "request_id": request.get("request_id"), "error": exc.diagnostic}

    def _asset_dir(self, asset_id: str) -> Path:
        return self.root / valid_id(asset_id)

    def build_asset(self, request: dict) -> dict:
        validate_request(request)
        recipe = request["params"]
        asset_id, request_id = recipe["asset_id"], request["request_id"]
        asset_dir = self._asset_dir(asset_id)
        payload_hash = hashlib.sha256(canonical_bytes(request)).hexdigest()
        with asset_lock(asset_dir):
            ledger_path = asset_dir / "requests" / (valid_id(request_id) + ".json")
            if ledger_path.exists():
                ledger = read_json(ledger_path)
                if ledger["payload_sha256"] != payload_hash:
                    raise ContractError("E_REQUEST_CONFLICT", "request_id was used with a different payload; use a new request_id")
                if (ledger.get("result") or {}).get("ok"):
                    result = ledger["result"]
                    # A ledger entry prepared before a failed publication is not a
                    # completed idempotent request. Only a committed marker proves it.
                    pointer_path = asset_dir / "latest.json"
                    pointer = read_json(pointer_path) if pointer_path.exists() else {}
                    committed = pointer.get("completed_requests", {}).get(request_id, {})
                    if committed.get("payload_sha256") != payload_hash or committed.get("revision") != result["revision"]:
                        result = None
                    else:
                        _, cached_dir, cached_manifest, _ = self._resolve(asset_id, result["revision"])
                        self._verify_packet(cached_dir, cached_manifest)
                        result["cached"] = True
                        result["is_latest"] = self._latest_revision(asset_dir) == result["revision"]
                        return result
            current = self._latest_revision(asset_dir)
            if "expected_revision" in request and request["expected_revision"] != current:
                raise ContractError("E_STALE_REVISION", "Asset changed since the observed revision", expected=request["expected_revision"], actual=current)
            impl_hash = implementation_digest()
            build_hash = hashlib.sha256(canonical_bytes({"recipe": recipe, "implementation_sha256": impl_hash})).hexdigest()
            revision = build_hash[:16]
            revision_dir = asset_dir / "revisions" / revision
            attempt = {"request_id": request_id, "revision": revision, "started_at": now(), "status": "building"}
            write_json(asset_dir / "latest_attempt.json", attempt)
            write_json(ledger_path, {"payload_sha256": payload_hash, "result": None})
            # Every successful revision is immutable. Content cache is safe only if every artifact still matches.
            if (revision_dir / "manifest.json").exists():
                cache_phase = "read"
                try:
                    manifest = read_json(revision_dir / "manifest.json")
                    pointer = read_json(asset_dir / "latest.json") if (asset_dir / "latest.json").exists() else {}
                    cache_phase = "validation"
                    committed_hash = pointer.get("revision_hashes", {}).get(revision)
                    if committed_hash is not None and committed_hash != file_sha256(revision_dir / "manifest.json"):
                        raise ContractError("E_MANIFEST_HASH", "Published manifest changed after commit")
                    if manifest.get("build_sha256") != build_hash or not all(
                        (revision_dir / x["path"]).is_file() and file_sha256(revision_dir / x["path"]) == x["sha256"]
                        for x in manifest["artifacts"].values()
                    ):
                        raise ContractError("E_REVISION_CORRUPT", "Immutable cached revision was modified; preserve it and rebuild in a new output root")
                    self._verify_packet(revision_dir, manifest)
                    cache_phase = "publication"
                    return self._publish(asset_dir, revision_dir, manifest, request_id, ledger_path, payload_hash, cached=True)
                except Exception as exc:
                    error = exc.diagnostic if isinstance(exc, ContractError) else {
                        "code": "E_PUBLICATION" if cache_phase == "publication" else "E_REVISION_CORRUPT",
                        "message": str(exc), "phase": cache_phase,
                    }
                    attempt.update(status="failed", finished_at=now(), error=error,
                                   previous_success_revision=current, diagnostics_directory=str(revision_dir))
                    result = {"ok": False, "asset_id": asset_id, "request_id": request_id, "error": error,
                              "previous_success_revision": current, "diagnostics_directory": str(revision_dir)}
                    # Publication has no writes after its atomic latest-pointer
                    # commit. Failures here therefore preserve the old pointer.
                    # A second filesystem error must not hide the primary error.
                    for failure_path, value in (
                        (asset_dir / "latest_attempt.json", attempt),
                        (ledger_path, {"payload_sha256": payload_hash, "result": result}),
                    ):
                        try:
                            write_json(failure_path, value)
                        except OSError as persistence_error:
                            result.setdefault("persistence_errors", []).append({
                                "file": failure_path.name, "message": str(persistence_error),
                            })
                    return result
            stage = asset_dir / ".staging" / (revision + "_" + uuid.uuid4().hex[:8])
            stage.mkdir(parents=True)
            write_json(stage / "request.json", request)
            try:
                self._run_worker(stage, revision, impl_hash)
                result_path = stage / "worker_result.json"
                worker_result = read_json(result_path) if result_path.exists() else {}
                if not worker_result.get("ok"):
                    detail = worker_result.get("error", {"code": "E_WORKER", "message": "Worker returned no valid result"})
                    raise ContractError(detail.get("code", "E_WORKER"), detail.get("message", "Build failed"), worker=detail)
                if implementation_digest() != impl_hash:
                    raise ContractError("E_SOURCE_CHANGED", "Implementation changed during build; retry against the finished source")
                artifacts = self._collect_artifacts(stage)
                manifest = {"asset_id": asset_id, "revision": revision, "build_sha256": build_hash,
                            "recipe_sha256": hashlib.sha256(canonical_bytes(recipe)).hexdigest(),
                            "implementation_sha256": impl_hash, "catalog_sha256": catalog_digest(),
                            "created_at": now(), "runtime": worker_result.get("runtime"),
                            "artifacts": artifacts, "validation": worker_result.get("validation", {})}
                write_json(stage / "manifest.json", manifest)
                self._verify_packet(stage, manifest)
                revision_dir.parent.mkdir(parents=True, exist_ok=True)
                # Verify both absolute directory targets before moving a populated Windows directory.
                if not stage.resolve().is_relative_to(self.root) or not revision_dir.resolve().is_relative_to(self.root):
                    raise ContractError("E_OUTPUT_PATH", "Publication target is outside output root")
                stage.rename(revision_dir)
                return self._publish(asset_dir, revision_dir, manifest, request_id, ledger_path, payload_hash, cached=False)
            except Exception as exc:
                detail = exc.diagnostic if isinstance(exc, ContractError) else {"code": "E_BUILD", "message": str(exc)}
                attempt.update(status="failed", finished_at=now(), error=detail, previous_success_revision=current,
                               diagnostics_directory=str(stage))
                write_json(asset_dir / "latest_attempt.json", attempt)
                result = {"ok": False, "asset_id": asset_id, "request_id": request_id, "error": detail,
                          "previous_success_revision": current, "diagnostics_directory": str(stage)}
                write_json(ledger_path, {"payload_sha256": payload_hash, "result": result})
                return result

    def _run_worker(self, stage: Path, revision: str, impl_hash: str):
        args = ["--request", str(stage / "request.json"), "--output", str(stage), "--revision", revision,
                "--implementation-sha256", impl_hash]
        if self.backend == "bpy":
            cmd = [sys.executable, "-m", "b3dtile.worker", *args]
        elif self.backend == "cli" and self.blender_executable:
            entry = Path(__file__).with_name("blender_entry.py")
            cmd = [self.blender_executable, "--background", "--factory-startup", "--disable-autoexec",
                   "--python-exit-code", "1", "--python", str(entry), "--", *args]
        else:
            raise ContractError("E_BACKEND", "Use bpy, or cli with an explicit Blender executable")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        with (stage / "worker.log").open("w", encoding="utf-8") as log:
            try:
                # MCP owns stdin: inheriting its active protocol pipe can block
                # Windows Python initialization before the worker starts.
                completed = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                                           timeout=self.timeout, check=False, cwd=str(stage))
            except subprocess.TimeoutExpired as exc:
                raise ContractError("E_BUILD_TIMEOUT", "Worker exceeded build timeout", seconds=self.timeout) from exc
        if completed.returncode:
            raise ContractError("E_WORKER_EXIT", "Worker process failed; inspect worker.log", exit_code=completed.returncode)

    def _collect_artifacts(self, stage: Path) -> dict:
        mime = {".png": "image/png", ".glb": "model/gltf-binary", ".json": "application/json", ".blend": "application/x-blender", ".log": "text/plain"}
        result = {}
        for index, path in enumerate(sorted(stage.rglob("*"))):
            if not path.is_file() or path.suffix not in mime or path.name == "manifest.json":
                continue
            relative = path.relative_to(stage).as_posix()
            artifact_id = "a_" + hashlib.sha256(relative.encode()).hexdigest()[:12]
            result[artifact_id] = {"path": relative, "mime_type": mime[path.suffix], "sha256": file_sha256(path), "bytes": path.stat().st_size}
        return result

    def _publish(self, asset_dir, revision_dir, manifest, request_id, ledger_path, payload_hash, *, cached):
        revision = manifest["revision"]
        result = {"ok": True, "asset_id": manifest["asset_id"], "request_id": request_id, "revision": revision,
                  "cached": cached, "is_latest": True, "directory": str(revision_dir),
                  "observation": str(revision_dir / "observation.json"), "manifest": str(revision_dir / "manifest.json"),
                  "artifacts": manifest["artifacts"], "validation": manifest["validation"]}
        # Write every fallible sidecar before the single atomic commit point.
        # Readers infer a prepared request's completion from the latest pointer.
        write_json(ledger_path, {"payload_sha256": payload_hash, "result": result, "committed": False})
        write_json(asset_dir / "latest_attempt.json", {"request_id": request_id, "revision": revision, "status": "prepared", "finished_at": now()})
        previous = read_json(asset_dir / "latest.json") if (asset_dir / "latest.json").exists() else {}
        completed = previous.get("completed_requests", {})
        completed[request_id] = {"payload_sha256": payload_hash, "revision": revision}
        revision_hashes = previous.get("revision_hashes", {})
        manifest_hash = file_sha256(revision_dir / "manifest.json")
        revision_hashes[revision] = manifest_hash
        write_json(asset_dir / "latest.json", {"revision": revision, "manifest_sha256": manifest_hash,
                   "request_id": request_id, "published_at": now(), "completed_requests": completed,
                   "revision_hashes": revision_hashes})
        # No writes after the commit point. The pointer is sufficient to prove
        # completion; replay derives the state instead of relying on sidecar flags.
        return result

    @staticmethod
    def _latest_revision(asset_dir):
        path = asset_dir / "latest.json"
        return read_json(path)["revision"] if path.exists() else None

    def _resolve(self, asset_id: str, revision: str = "latest"):
        asset_dir = self._asset_dir(asset_id)
        pointer = read_json(asset_dir / "latest.json") if (asset_dir / "latest.json").exists() else {}
        latest = pointer.get("revision")
        resolved = latest if revision == "latest" else revision
        if resolved is None:
            raise ContractError("E_NO_SUCCESSFUL_BUILD", "No successfully published model exists", asset_id=asset_id)
        if not isinstance(resolved, str) or not re.fullmatch(r"[0-9a-f]{16}", resolved):
            raise ContractError("E_REVISION", "Invalid revision")
        directory = asset_dir / "revisions" / resolved
        if not (directory / "manifest.json").is_file():
            raise ContractError("E_REVISION_UNKNOWN", "Revision does not exist", revision=resolved)
        manifest_path = directory / "manifest.json"
        manifest = read_json(manifest_path)
        if manifest.get("asset_id") != asset_id or manifest.get("revision") != resolved:
            raise ContractError("E_MANIFEST_IDENTITY", "Manifest identity does not match its asset and revision directory")
        expected_hash = pointer.get("manifest_sha256") if resolved == latest else pointer.get("revision_hashes", {}).get(resolved)
        if expected_hash is None:
            raise ContractError("E_REVISION_UNPUBLISHED", "Revision was never committed")
        if file_sha256(manifest_path) != expected_hash:
            raise ContractError("E_MANIFEST_HASH", "Published manifest changed after commit")
        return asset_dir, directory, manifest, latest

    @staticmethod
    def _verify_artifacts(directory, manifest):
        for entry in manifest["artifacts"].values():
            path = (directory / entry["path"]).resolve()
            if not path.is_relative_to(directory.resolve()) or not path.is_file() or file_sha256(path) != entry["sha256"]:
                raise ContractError("E_ARTIFACT_HASH", "Published artifact is missing or has changed", artifact=entry["path"])

    @classmethod
    def _verify_packet(cls, directory, manifest):
        cls._verify_artifacts(directory, manifest)
        observation = read_json(directory / "observation.json")
        if observation.get("revision") != manifest["revision"] or observation.get("asset_id") != manifest["asset_id"]:
            raise ContractError("E_OBSERVATION_REVISION", "Observation identity differs from build identity")
        paths = {v["path"]: v for v in manifest["artifacts"].values()}
        glb = observation.get("glb", manifest["asset_id"] + ".glb")
        if glb not in paths or paths[glb]["mime_type"] != "model/gltf-binary":
            raise ContractError("E_GLB_MISSING", "Observation packet has no exported GLB")
        if not observation.get("images") or observation.get("contact_sheet") not in paths:
            raise ContractError("E_PREVIEW_MISSING", "Observation packet needs preview images and a contact sheet")
        for image in observation["images"]:
            artifact = paths.get(image.get("path"))
            if image.get("revision") != manifest["revision"] or artifact is None or artifact["mime_type"] != "image/png" or artifact["sha256"] != image.get("sha256"):
                raise ContractError("E_PREVIEW_REVISION", "Preview metadata/hash does not match its immutable build", image=image.get("path"))

    def observe_asset(self, asset_id: str, revision: str = "latest", known_revision: str | None = None, detail: str = "summary") -> dict:
        asset_dir, directory, manifest, latest = self._resolve(asset_id, revision)
        observation_path = directory / "observation.json"
        entry = next((a for a in manifest["artifacts"].values() if a["path"] == "observation.json"), None)
        if entry is None or not observation_path.is_file() or file_sha256(observation_path) != entry["sha256"]:
            raise ContractError("E_ARTIFACT_HASH", "Observation was modified after publication")
        result = read_json(observation_path)
        if result.get("revision") != manifest["revision"]:
            raise ContractError("E_OBSERVATION_REVISION", "Observation does not match its manifest")
        attempt_path = asset_dir / "latest_attempt.json"
        attempt = read_json(attempt_path) if attempt_path.exists() else {}
        pointer = read_json(asset_dir / "latest.json") if (asset_dir / "latest.json").exists() else {}
        attempt_status = attempt.get("status")
        if attempt_status == "prepared" and pointer.get("request_id") == attempt.get("request_id") and pointer.get("revision") == attempt.get("revision"):
            attempt_status = "succeeded"
        elif attempt_status == "prepared":
            attempt_status = "publication_pending"
        result["freshness"] = {"resolved_revision": manifest["revision"], "latest_success_revision": latest,
                               "is_latest_success": manifest["revision"] == latest,
                               "changed_since_known_revision": known_revision != manifest["revision"],
                               "latest_attempt_status": attempt_status,
                               "latest_attempt_error": attempt.get("error"),
                               "note": "Snapshot of a recipe build; external .blend edits are not monitored."}
        result["artifacts"] = manifest["artifacts"]
        result["next_build_expected_revision"] = latest
        artifact_ids = {entry["path"]: aid for aid, entry in manifest["artifacts"].items()}
        for image in result.get("images", []):
            image["artifact_id"] = artifact_ids.get(image["path"])
        if known_revision and known_revision != manifest["revision"]:
            _, previous_dir, previous_manifest, _ = self._resolve(asset_id, known_revision)
            self._verify_artifacts(previous_dir, previous_manifest)
            previous = read_json(previous_dir / "observation.json")
            before = {x["id"]: x for x in previous.get("objects", [])}
            after = {x["id"]: x for x in result.get("objects", [])}
            changes = []
            for oid in sorted(before.keys() & after.keys()):
                fields = [field for field in ("kit_id", "world_matrix", "geometry_sha256", "appearance_sha256", "regions", "bone_count") if before[oid].get(field) != after[oid].get(field)]
                if fields:
                    changes.append({"object_id": oid, "fields": fields})
            result["changes"] = {"from_revision": known_revision, "added": sorted(after.keys() - before.keys()), "removed": sorted(before.keys() - after.keys()), "changed": changes}
        if detail == "summary":
            result["objects"] = [{key: obj[key] for key in ("id", "kit_id", "bbox", "vertices", "triangles", "bone_count", "id_color_rgb") if key in obj} for obj in result.get("objects", [])]
            result["images"] = [{k: v for k, v in image.items() if k not in ("objects", "camera_matrix", "camera_world_matrix", "projection_matrix")} for image in result.get("images", [])]
            result["artifacts"] = {aid: item for aid, item in manifest["artifacts"].items() if item["mime_type"] in ("image/png", "model/gltf-binary")}
            reports = result.get("diagnostics", {})
            result["diagnostics"] = {"geometry_ok": reports.get("geometry", {}).get("ok"),
                                     "uv_ok": all(v.get("ok") for v in reports.get("uv", {}).values()),
                                     "rig_ok": all(v.get("ok") for v in reports.get("rig", {}).values()),
                                     "glb_ok": reports.get("glb", {}).get("ok"),
                                     "khronos": reports.get("khronos", {})}
        return result

    def inspect_object(self, asset_id: str, revision: str, object_id: str) -> dict:
        observation = self.observe_asset(asset_id, revision, detail="full")
        objects = observation["objects"]
        if isinstance(objects, dict):
            obj = objects.get(object_id)
        else:
            obj = next((x for x in objects if x.get("object_id", x.get("id")) == object_id), None)
        if obj is None:
            raise ContractError("E_OBJECT_UNKNOWN", "Object is absent from this observation", object_id=object_id)
        images = []
        for original in observation.get("images", []):
            image = dict(original)
            if isinstance(image.get("objects"), dict):
                image["objects"] = {object_id: image["objects"][object_id]} if object_id in image["objects"] else {}
            images.append(image)
        return {"revision": observation["revision"], "object": obj, "freshness": observation["freshness"], "images": images}

    def artifact_path(self, asset_id: str, revision: str, artifact_id: str, *, images_only: bool = False) -> Path:
        if revision == "latest":
            raise ContractError("E_EXPLICIT_REVISION", "Read a resolved revision from observe_asset before reading its images")
        _, directory, manifest, _ = self._resolve(asset_id, revision)
        entry = manifest["artifacts"].get(artifact_id)
        if entry is None:
            raise ContractError("E_ARTIFACT_UNKNOWN", "Use artifact IDs from observe_asset")
        if images_only and entry["mime_type"] != "image/png":
            raise ContractError("E_ARTIFACT_TYPE", "This tool only returns preview images")
        path = (directory / entry["path"]).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file() or file_sha256(path) != entry["sha256"]:
            raise ContractError("E_ARTIFACT_HASH", "Artifact is missing or changed after publication")
        return path

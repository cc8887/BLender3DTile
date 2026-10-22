"""Publication/freshness regressions using a tiny deterministic fake worker.

These exercise real service persistence and hashes without starting Blender.
The fake emits the same required artifact structure as the production worker.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from b3dtile import service as service_module
from b3dtile.contracts import ContractError, canonical_bytes, catalog_digest, file_sha256
from b3dtile.service import AssetService, read_json, write_json


@pytest.fixture
def setup_service(tmp_path, monkeypatch):
    service = AssetService(tmp_path / "assets")
    state = {"implementation": "a" * 64, "runs": 0, "fail": False,
             "wrong_image_revision": False}
    monkeypatch.setattr(service_module, "implementation_digest", lambda: state["implementation"])

    def fake_worker(stage, revision, impl_hash):
        state["runs"] += 1
        if state["fail"]:
            write_json(stage / "worker_result.json", {"ok": False, "error": {
                "code": "E_FAKE_WORKER", "message": "Injected worker failure"}})
            return
        request = read_json(stage / "request.json")
        asset_id = request["params"]["asset_id"]
        (stage / "observations").mkdir()
        preview = stage / "observations" / "beauty_front_rest.png"
        Image.new("RGB", (4, 4), (request["params"]["seed"] % 256, 40, 80)).save(preview)
        (stage / (asset_id + ".glb")).write_bytes(b"fake GLB; worker boundary only")
        (stage / "debug.blend").write_bytes(b"fake blend")
        write_json(stage / "model_data.json", {"walls": {"kit_id": "house_shell_2m_v1"}})
        validation = {"geometry": {"ok": True}, "glb": {"ok": True}}
        write_json(stage / "validation.json", validation)
        write_json(stage / "observation.json", {
            "observation_version": "1.0", "asset_id": asset_id, "revision": revision,
            "objects": [{"id": "walls", "kit_id": "house_shell_2m_v1",
                         "geometry_sha256": "a" * 64, "appearance_sha256": file_sha256(preview)}],
            "images": [{"path": "observations/beauty_front_rest.png",
                        "revision": "0" * 16 if state["wrong_image_revision"] else revision,
                        "sha256": "0" * 64 if state.get("wrong_image_hash") else file_sha256(preview), "dimensions": [4, 4],
                        "camera": "front", "pose": "rest", "mode": "beauty"}],
            "contact_sheet": "observations/beauty_front_rest.png", "diagnostics": validation,
            "glb": asset_id + ".glb",
        })
        write_json(stage / "worker_result.json", {
            "ok": True, "runtime": {"blender": "4.5.14", "python": "3.11"},
            "validation": validation,
        })
        if state.get("missing_preview"):
            preview.unlink()
        if state.get("missing_glb"):
            (stage / (asset_id + ".glb")).unlink()
        if state.get("source_changes_during_worker"):
            state["implementation"] = "c" * 64

    monkeypatch.setattr(service, "_run_worker", fake_worker)
    template = json.loads((Path(__file__).parents[1] / "examples" / "house.request.json").read_text())

    def request(request_id="request_one", seed=42, **envelope):
        result = deepcopy(template)
        result["request_id"] = request_id
        result["params"]["asset_id"] = "test_house"
        result["params"]["catalog_sha256"] = catalog_digest()
        result["params"]["seed"] = seed
        result.update(envelope)
        return result

    return service, state, request


def build_result(service, request):
    """Public failures may originate before or during the staged worker call."""
    try:
        return service.build_asset(request)
    except ContractError as error:
        return {"ok": False, "error": error.diagnostic}


def preview_artifact(result):
    return next(key for key, value in result["artifacts"].items()
                if value["path"] == "observations/beauty_front_rest.png")


def test_idempotent_retry_preserves_original_revision_without_rebuilding(setup_service):
    service, state, request = setup_service
    original_request = request()
    first = service.build_asset(original_request)
    retry = service.build_asset(original_request)
    assert retry["ok"] and retry["cached"] and retry["revision"] == first["revision"]
    assert state["runs"] == 1


def test_same_request_id_rejects_different_payload(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    conflict = build_result(service, request(seed=43))
    assert not conflict["ok"] and conflict["error"]["code"] == "E_REQUEST_CONFLICT"
    assert service.observe_asset("test_house")["revision"] == first["revision"]


def test_stale_guard_keeps_latest_and_does_not_launch_worker(setup_service):
    service, state, request = setup_service
    first = service.build_asset(request())
    stale = build_result(service, request("request_two", 43, expected_revision="0" * 16))
    assert not stale["ok"] and stale["error"]["code"] == "E_STALE_REVISION"
    assert state["runs"] == 1
    assert service.observe_asset("test_house")["revision"] == first["revision"]


def test_failed_build_keeps_previous_images_and_reports_failed_attempt(setup_service):
    service, state, request = setup_service
    first = service.build_asset(request())
    previous_image = service.artifact_path("test_house", first["revision"], preview_artifact(first)).read_bytes()
    state["fail"] = True
    failure = service.build_asset(request("request_two", 43))
    assert not failure["ok"] and failure["previous_success_revision"] == first["revision"]
    latest = service.observe_asset("test_house", known_revision=first["revision"])
    assert latest["revision"] == first["revision"]
    assert latest["freshness"]["latest_attempt_status"] == "failed"
    assert not latest["freshness"]["changed_since_known_revision"]
    assert service.artifact_path("test_house", first["revision"], preview_artifact(first)).read_bytes() == previous_image


def test_explicit_revision_and_latest_keep_their_images_consistent(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    second = service.build_asset(request("request_two", 43, expected_revision=first["revision"]))
    old = service.inspect_object("test_house", first["revision"], "walls")
    latest = service.observe_asset("test_house")
    assert old["revision"] == first["revision"] != second["revision"]
    assert not old["freshness"]["is_latest_success"]
    assert latest["revision"] == second["revision"]
    assert all(image["revision"] == first["revision"] for image in old["images"])
    assert all(image["revision"] == second["revision"] for image in latest["images"])


def test_new_request_uses_current_implementation_but_retry_stays_pinned(setup_service):
    service, state, request = setup_service
    original_request = request()
    first = service.build_asset(original_request)
    state["implementation"] = "b" * 64
    second = service.build_asset(request("request_two"))
    assert second["revision"] != first["revision"] and state["runs"] == 2
    replay = service.build_asset(original_request)
    assert replay["revision"] == first["revision"]
    assert replay["cached"] and not replay["is_latest"]
    assert state["runs"] == 2


def test_read_preview_rejects_tampered_image(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    artifact_id = preview_artifact(first)
    path = service.artifact_path("test_house", first["revision"], artifact_id)
    path.write_bytes(b"tampered")
    with pytest.raises(ContractError) as error:
        service.artifact_path("test_house", first["revision"], artifact_id, images_only=True)
    assert error.value.diagnostic["code"] == "E_ARTIFACT_HASH"


def test_idempotent_retry_rejects_corrupted_cached_revision(setup_service):
    service, _, request = setup_service
    original = request()
    first = service.build_asset(original)
    image = service.artifact_path("test_house", first["revision"], preview_artifact(first))
    image.write_bytes(b"tampered")
    replay = build_result(service, original)
    assert not replay["ok"], "A cached successful ledger entry must not bypass artifact verification"


def test_manifest_hash_is_verified_against_atomic_latest_pointer(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    path = Path(first["manifest"])
    manifest = read_json(path)
    manifest["runtime"]["blender"] = "unexpected runtime"
    write_json(path, manifest)
    with pytest.raises(ContractError):
        service.observe_asset("test_house")


def test_corrupt_content_cache_does_not_leave_attempt_building(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    service.artifact_path("test_house", first["revision"], preview_artifact(first)).write_bytes(b"tampered")
    retry = build_result(service, request("request_two"))
    assert not retry["ok"]
    attempt = read_json(service.root / "test_house" / "latest_attempt.json")
    assert attempt["status"] == "failed", "A rejected content cache is a completed failed attempt"


def test_worker_mismatched_image_revision_is_rejected_before_publication(setup_service):
    service, state, request = setup_service
    first = service.build_asset(request())
    state["wrong_image_revision"] = True
    bad = build_result(service, request("request_two", 43))
    assert not bad["ok"], "Publishing mixed revision metadata would make previews unsafe for AI decisions"
    assert service._latest_revision(service.root / "test_house") == first["revision"]


def test_failed_publication_does_not_advance_latest_pointer(setup_service, monkeypatch):
    service, _, request = setup_service
    first = service.build_asset(request())
    real_write = service_module.write_json
    injected = False

    def fail_status_once(path, value):
        nonlocal injected
        if path.name == "latest_attempt.json" and value.get("status") in ("prepared", "succeeded") and not injected:
            injected = True
            raise OSError("Injected failure while finalizing publication")
        return real_write(path, value)

    monkeypatch.setattr(service_module, "write_json", fail_status_once)
    result = build_result(service, request("request_two", 43))
    assert injected and not result["ok"]
    assert service._latest_revision(service.root / "test_house") == first["revision"]


def test_invalid_asset_path_never_creates_output(setup_service):
    service, _, _ = setup_service
    with pytest.raises(ContractError):
        service.observe_asset("../escape")
    assert not service.root.exists()


@pytest.mark.parametrize("fault", ["wrong_image_hash", "missing_preview", "missing_glb"])
def test_incomplete_or_inconsistent_packet_never_replaces_latest(setup_service, fault):
    service, state, request = setup_service
    first = service.build_asset(request())
    state[fault] = True
    failure = build_result(service, request("request_two", 43))
    assert not failure["ok"]
    assert service._latest_revision(service.root / "test_house") == first["revision"]


def test_source_change_during_worker_never_publishes_mixed_build(setup_service):
    service, state, request = setup_service
    first = service.build_asset(request())
    state["source_changes_during_worker"] = True
    failure = build_result(service, request("request_two", 43))
    assert not failure["ok"] and failure["error"]["code"] == "E_SOURCE_CHANGED"
    assert service._latest_revision(service.root / "test_house") == first["revision"]


def test_retry_resumes_a_request_interrupted_before_worker_finished(setup_service):
    service, state, request = setup_service
    original = request()
    ledger = service.root / "test_house" / "requests" / "request_one.json"
    write_json(ledger, {"payload_sha256": hashlib.sha256(canonical_bytes(original)).hexdigest(), "result": None})
    recovered = service.build_asset(original)
    assert recovered["ok"] and state["runs"] == 1


def test_content_cache_cannot_recommit_a_tampered_manifest(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    manifest_path = Path(first["manifest"])
    manifest = read_json(manifest_path)
    manifest["runtime"]["blender"] = "corrupted runtime provenance"
    write_json(manifest_path, manifest)
    replay = build_result(service, request("request_two"))
    assert not replay["ok"], "Content cache must use the previously committed manifest fingerprint"


def test_known_revision_diff_reports_actual_appearance_change(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    second = service.build_asset(request("request_two", 43))
    observed = service.observe_asset("test_house", known_revision=first["revision"])
    assert observed["revision"] == second["revision"]
    assert observed["changes"] == {"from_revision": first["revision"], "added": [], "removed": [],
                                   "changed": [{"object_id": "walls", "fields": ["appearance_sha256"]}]}


def test_historical_manifest_hash_is_checked_after_a_new_build(setup_service):
    service, _, request = setup_service
    first = service.build_asset(request())
    service.build_asset(request("request_two", 43))
    path = Path(first["manifest"])
    manifest = read_json(path)
    manifest["runtime"]["blender"] = "changed historical runtime"
    write_json(path, manifest)
    with pytest.raises(ContractError):
        service.observe_asset("test_house", revision=first["revision"])


def test_reader_uses_one_pointer_snapshot_during_concurrent_publication(setup_service, monkeypatch):
    service, _, request = setup_service
    first = service.build_asset(request())
    pointer_path = service.root / "test_house" / "latest.json"
    old_pointer = read_json(pointer_path)
    second = service.build_asset(request("request_two", 43))
    new_pointer = read_json(pointer_path)
    write_json(pointer_path, old_pointer)
    real_read = service_module.read_json
    switched = False

    def read_and_publish(path):
        nonlocal switched
        value = real_read(path)
        if path == pointer_path and not switched:
            switched = True
            write_json(pointer_path, new_pointer)
        return value

    monkeypatch.setattr(service_module, "read_json", read_and_publish)
    observed = service.observe_asset("test_house")
    assert switched
    assert observed["revision"] in (first["revision"], second["revision"])
    assert all(image["revision"] == observed["revision"] for image in observed["images"])


def test_malformed_cache_manifest_records_failed_attempt_and_preserves_latest(setup_service):
    service, state, request = setup_service
    cached = service.build_asset(request())
    current = service.build_asset(request("request_two", 43))
    pointer_path = service.root / "test_house" / "latest.json"
    original_pointer = pointer_path.read_bytes()
    Path(cached["manifest"]).write_text("{invalid JSON", encoding="utf-8")

    result = service.execute(request("request_three"))

    assert not result["ok"] and result["error"]["code"] == "E_JSON"
    assert result["previous_success_revision"] == current["revision"]
    assert pointer_path.read_bytes() == original_pointer
    assert state["runs"] == 2
    attempt = read_json(service.root / "test_house" / "latest_attempt.json")
    assert attempt["status"] == "failed" and attempt["error"]["code"] == "E_JSON"
    ledger = read_json(service.root / "test_house" / "requests" / "request_three.json")
    assert ledger["result"] == result


def test_cache_publication_oserror_is_structured_and_preserves_latest(setup_service, monkeypatch):
    service, state, request = setup_service
    service.build_asset(request())
    current = service.build_asset(request("request_two", 43))
    pointer_path = service.root / "test_house" / "latest.json"
    original_pointer = pointer_path.read_bytes()
    real_write = service_module.write_json
    injected = False

    def fail_cached_prepare_once(path, value):
        nonlocal injected
        if path.name == "latest_attempt.json" and value.get("status") == "prepared" and not injected:
            injected = True
            raise OSError("Injected cache publication failure")
        return real_write(path, value)

    monkeypatch.setattr(service_module, "write_json", fail_cached_prepare_once)
    result = service.execute(request("request_three"))

    assert injected and not result["ok"] and result["error"]["code"] == "E_PUBLICATION"
    assert result["previous_success_revision"] == current["revision"]
    assert pointer_path.read_bytes() == original_pointer
    assert state["runs"] == 2
    attempt = read_json(service.root / "test_house" / "latest_attempt.json")
    assert attempt["status"] == "failed" and attempt["error"]["code"] == "E_PUBLICATION"
    ledger = read_json(service.root / "test_house" / "requests" / "request_three.json")
    assert ledger["result"] == result

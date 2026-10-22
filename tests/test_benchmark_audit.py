"""Benchmark limits and evidence, using the production MCP request handler."""
import asyncio
import base64
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest
from PIL import Image

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolRequest, CallToolRequestParams, ImageContent, TextContent

from b3dtile.contracts import catalog_digest, file_sha256
from b3dtile.service import AssetService, read_json, write_json

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("benchmark_audit_server", ROOT / "benchmarks" / "audit_server.py")
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def completed(path):
    return [entry for entry in records(path) if entry["event"] == "tool_call"]


def text_result(result):
    body = getattr(result, "root", result)
    return json.loads(next(item.text for item in body.content if isinstance(item, TextContent)))


def call(server, name, arguments):
    request = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
    return asyncio.run(server.request_handlers[CallToolRequest](request))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    state = {"fail": False, "worker_runs": 0}

    def fake_worker(self, stage, revision, impl_hash):
        state["worker_runs"] += 1
        if state["fail"]:
            write_json(stage / "worker_result.json", {"ok": False, "error": {"code": "E_FAKE_WORKER", "message": "Failed worker"}})
            return
        request = read_json(stage / "request.json")
        asset_id = request["params"]["asset_id"]
        preview = stage / "contact_sheet.png"
        Image.new("RGB", (4, 4), (30, 70, 130)).save(preview)
        (stage / (asset_id + ".glb")).write_bytes(b"fake GLB; testing worker boundary")
        write_json(stage / "observation.json", {
            "asset_id": asset_id, "revision": revision, "contact_sheet": preview.name,
            "glb": asset_id + ".glb", "objects": [{"id": "walls", "regions": {}}],
            "images": [{"path": preview.name, "revision": revision, "sha256": file_sha256(preview),
                        "mode": "contact_sheet", "camera": "front", "pose": "rest"}],
            "diagnostics": {"geometry": {"ok": True}, "glb": {"ok": True}},
        })
        write_json(stage / "worker_result.json", {"ok": True, "validation": {"ok": True}})

    monkeypatch.setattr(AssetService, "_run_worker", fake_worker)
    template = json.loads((ROOT / "examples" / "house.request.json").read_text())

    def arguments(request_id="first_request", seed=42, asset_id="benchmark_house"):
        recipe = deepcopy(template["params"])
        recipe.update(asset_id=asset_id, seed=seed, catalog_sha256=catalog_digest())
        return {"request_id": request_id, "recipe": recipe}

    def create(**limits):
        path = tmp_path / "audit.jsonl"
        server, audit = audit_module.make_audited_server(ROOT, tmp_path / "out", path,
                                                        "benchmark_house", **limits)
        return server, audit, path

    return create, arguments, state, tmp_path


def test_build_attempts_include_success_failure_cache_and_preserve_full_recipe(fixture):
    create, args, state, _ = fixture
    server, audit, path = create()
    first = args()
    assert text_result(call(server, "build_asset", first))["ok"]
    assert text_result(call(server, "build_asset", first))["cached"]
    state["fail"] = True
    assert text_result(call(server, "build_asset", args("second_request", 43)))["error"]["code"] == "E_FAKE_WORKER"
    denied = text_result(call(server, "build_asset", args("third_request", 44)))
    assert denied["error"]["code"] == "E_BENCHMARK_BUILD_BUDGET"
    entries = completed(path)
    assert len(entries) == 4
    assert [x["build_attempts"] for x in entries] == [1, 2, 3, 3]
    assert [x["worker_runs"] for x in entries] == [1, 0, 1, 0]
    assert [x["ok"] for x in entries] == [True, True, False, False]
    assert entries[0]["input"] == first
    assert entries[0]["result_summary"]["revision"]
    assert state["worker_runs"] == 2
    assert audit.calls == 4 and audit.build_attempts == 3


def test_invalid_schema_and_runtime_validation_both_consume_attempts(fixture):
    create, args, state, _ = fixture
    server, audit, path = create(max_builds=2)
    malformed = call(server, "build_asset", {"request_id": "missing_recipe"})
    assert malformed.root.isError
    bad_runtime = args()
    bad_runtime["recipe"]["ops"].insert(0, deepcopy(bad_runtime["recipe"]["ops"][0]))
    runtime_failure = call(server, "build_asset", bad_runtime)
    assert runtime_failure.root.isError
    assert text_result(call(server, "build_asset", args()))["error"]["code"] == "E_BENCHMARK_BUILD_BUDGET"
    entries = completed(path)
    assert [x["build_attempts"] for x in entries] == [1, 2, 2]
    assert entries[0]["error"]["code"] == "E_MCP_INPUT_OR_TOOL"
    assert entries[1]["error"]["code"] != "E_MCP_INPUT_OR_TOOL"
    assert audit.calls == 3 and state["worker_runs"] == 0


@pytest.mark.parametrize("name,args", [
    ("observe_asset", {"asset_id": "other_asset"}),
    ("inspect_object", {"asset_id": "other_asset", "revision": "0" * 16, "object_id": "walls"}),
    ("read_preview", {"asset_id": "other_asset", "revision": "0" * 16, "artifact_id": "a_0123456789ab"}),
])
def test_cross_asset_reads_are_rejected(fixture, name, args):
    create, _, _, _ = fixture
    server, _, path = create()
    assert text_result(call(server, name, args))["error"]["code"] == "E_BENCHMARK_ASSET"
    assert completed(path)[0]["input"] == args


def test_cross_asset_build_is_rejected_before_worker(fixture):
    create, args, state, _ = fixture
    server, audit, path = create()
    result = call(server, "build_asset", args(asset_id="other_asset"))
    assert text_result(result)["error"]["code"] == "E_BENCHMARK_ASSET"
    assert audit.build_attempts == 1 and state["worker_runs"] == 0
    assert not completed(path)[0]["ok"]


def test_call_budget_includes_unknown_tools_and_does_not_spend_build_budget_after_limit(fixture):
    create, args, _, _ = fixture
    server, audit, path = create(max_calls=2)
    assert not call(server, "get_catalog", {}).root.isError
    assert call(server, "unknown_tool", {}).root.isError
    assert text_result(call(server, "build_asset", args()))["error"]["code"] == "E_BENCHMARK_CALL_BUDGET"
    assert audit.calls == 3 and audit.build_attempts == 0
    assert [x["tool_call_number"] for x in completed(path)] == [1, 2, 3]


def test_nested_inspect_only_counts_once_and_native_preview_has_exact_revision_hash(fixture):
    create, args, _, _ = fixture
    server, audit, path = create()
    built = text_result(call(server, "build_asset", args()))
    observed = text_result(call(server, "observe_asset", {"asset_id": "benchmark_house"}))
    revision = observed["revision"]
    assert revision == built["revision"]
    inspected = text_result(call(server, "inspect_object", {
        "asset_id": "benchmark_house", "revision": revision, "object_id": "walls"}))
    assert inspected["object"]["id"] == "walls"
    artifact_id = observed["images"][0]["artifact_id"]
    preview = call(server, "read_preview", {"asset_id": "benchmark_house", "revision": revision, "artifact_id": artifact_id})
    content = preview.root.content
    assert len(content) == 1 and isinstance(content[0], ImageContent)
    entries = completed(path)
    assert len(entries) == 4 and audit.calls == 4
    evidence = entries[-1]["evidence"]
    assert evidence["revision"] == revision and evidence["artifact_id"] == artifact_id
    assert evidence["sha256"] == observed["images"][0]["sha256"]
    assert base64.b64decode(content[0].data) == Path(evidence["path"]).read_bytes()
    assert entries[-1]["result_summary"]["native_image"]
    assert entries[2]["tool"] == "inspect_object" and entries[2]["evidence"]["resolved_revision"] == revision


def test_stdio_entry_preserves_enumerated_tools_and_audits_schema_rejection(tmp_path):
    path = tmp_path / "audit.jsonl"

    async def check():
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "benchmarks" / "audit_server.py"),
            "--backend-root", str(ROOT), "--output-root", str(tmp_path / "out"), "--audit", str(path),
            "--asset-id", "benchmark_house", "--max-builds", "3", "--max-calls", "40"],
            env={**os.environ, "PYTHONUTF8": "1"}, cwd=str(tmp_path))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert len(tools) == 5
                assert "humanoid_round_v1" in tools["build_asset"].inputSchema["$defs"]["kit_id"]["enum"]
                assert not (await session.call_tool("get_catalog", {})).isError
                assert (await session.call_tool("read_preview", {
                    "asset_id": "benchmark_house", "revision": "latest", "artifact_id": "a_0123456789ab"})).isError

    asyncio.run(asyncio.wait_for(check(), timeout=35))
    entries = completed(path)
    assert len(entries) == 2
    assert entries[-1]["error"]["code"] == "E_MCP_INPUT_OR_TOOL"
    assert entries[-1]["input"]["revision"] == "latest"

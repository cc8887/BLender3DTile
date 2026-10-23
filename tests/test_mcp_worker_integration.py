"""Real stdio -> audited MCP -> bpy worker regression for inherited stdin hangs."""
import asyncio
import base64
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent

from b3dtile.contracts import file_sha256
from b3dtile.glb import inspect_glb


ROOT = Path(__file__).parents[1]


@pytest.mark.blender
@pytest.mark.integration
@pytest.mark.skipif(importlib.util.find_spec("bpy") is None, reason="Requires the pinned bpy backend")
def test_real_audited_mcp_build_finishes_while_protocol_stdin_remains_open(tmp_path):
    """A real build must finish without waiting for the MCP input pipe to close.

    Windows previously left the worker at ~15 MB and zero log bytes until the
    service timed out. No subprocess or Blender behavior is mocked here.
    """
    if not (ROOT / ".tools/gltf-validator/node_modules/gltf-validator/package.json").exists():
        pytest.skip("Requires the official glTF validator installed by bootstrap")
    output = tmp_path / "assets"
    audit_path = tmp_path / "audit.jsonl"
    request = json.loads((ROOT / "examples/features_house.request.json").read_text(encoding="utf-8"))
    request["params"]["asset_id"] = "mcp_integration_house"

    def text_result(result):
        assert not result.isError, result
        return json.loads(next(item.text for item in result.content if isinstance(item, TextContent)))

    async def check():
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "benchmarks/audit_server.py"),
            "--backend-root", str(ROOT), "--output-root", str(output), "--audit", str(audit_path),
            "--asset-id", request["params"]["asset_id"], "--max-builds", "3", "--max-calls", "40"],
            env={**os.environ, "PYTHONUTF8": "1"}, cwd=str(tmp_path))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=90)) as session:
                await session.initialize()
                built = text_result(await session.call_tool("build_asset", {
                    "recipe": request["params"], "request_id": "real_mcp_build"}))
                assert built["ok"] and not built["cached"]
                directory = Path(built["directory"])
                # Publication is reached only after subprocess.returncode == 0.
                worker = json.loads((directory / "worker_result.json").read_text(encoding="utf-8"))
                assert worker["ok"]
                assert (directory / "worker.log").stat().st_size > 0
                glb_path = directory / (request["params"]["asset_id"] + ".glb")
                assert inspect_glb(glb_path)["ok"]
                assert built["validation"]["khronos"]["ok"]
                assert built["validation"]["glb"]["metrics"]["colored_primitives"] >= 1
                assert built["validation"]["glb"]["metrics"]["uv_animation_channels"] == 1
                observed = text_result(await session.call_tool("observe_asset", {"asset_id": request["params"]["asset_id"]}))
                assert observed["revision"] == built["revision"]
                assert any(item["mode"] == "vertex_color" for item in observed["images"])
                frames = [item for item in observed["images"] if "time_seconds" in item]
                assert [item["time_seconds"] for item in frames] == [0., .5, 1., 1.5]
                assert len({item["sha256"] for item in frames}) >= 3
                assert all(item["revision"] == built["revision"] for item in frames)
                image = next(item for item in observed["images"] if item["path"] == "observations/uv_animation_strip.png")
                preview = await session.call_tool("read_preview", {
                    "asset_id": request["params"]["asset_id"], "revision": observed["revision"], "artifact_id": image["artifact_id"]})
                assert not preview.isError and isinstance(preview.content[0], ImageContent)
                assert base64.b64decode(preview.content[0].data) == (directory / image["path"]).read_bytes()
                return directory, built["revision"], image["artifact_id"]

    directory, revision, artifact_id = asyncio.run(asyncio.wait_for(check(), timeout=110))
    completed = [record for line in audit_path.read_text(encoding="utf-8").splitlines()
                 if (record := json.loads(line))["event"] == "tool_call"]
    assert [record["tool"] for record in completed] == ["build_asset", "observe_asset", "read_preview"]
    assert all(record["ok"] for record in completed)
    assert completed[0]["worker_runs"] == 1
    assert completed[0]["result_summary"]["revision"] == revision
    evidence = completed[-1]["evidence"]
    assert evidence["revision"] == revision and evidence["artifact_id"] == artifact_id
    assert evidence["sha256"] == file_sha256(Path(evidence["path"]))

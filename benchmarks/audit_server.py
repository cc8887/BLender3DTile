"""Unmodified production MCP tools behind a benchmark-only audit boundary.

Every received tools/call counts against max_calls, including invalid schemas,
unknown tools and denied assets. A build attempt is an admitted build_asset call:
it consumes one of max_builds before schema/runtime validation, including cache
hits and failures. Budget-rejected calls do not consume another build attempt.
Actual worker launches are recorded separately. Nested service calls never count.
The service and its workers import only the explicitly selected frozen backend.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_backend(backend_root):
    root = Path(backend_root).resolve()
    package = root / "src" / "b3dtile"
    if not (package / "mcp_server.py").is_file():
        raise ValueError(f"Missing frozen backend at {package}")
    loaded = sys.modules.get("b3dtile")
    if loaded is not None and Path(loaded.__file__).resolve().parent != package:
        raise RuntimeError("A different b3dtile backend is already imported")
    sys.path.insert(0, str(root / "src"))
    return importlib.import_module("b3dtile.mcp_server")


class Audit:
    def __init__(self, path, asset_id, max_builds=3, max_calls=40):
        if max_builds < 1 or max_calls < 1:
            raise ValueError("Budgets must be positive")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.asset_id = asset_id
        self.max_builds, self.max_calls = max_builds, max_calls
        self.calls = self.build_attempts = 0
        # Starts are flushed before dispatch, so an interrupted call still uses
        # its budget. Aggregate events across sessions rather than trusting the
        # counters recorded by older servers that restarted counting from zero.
        # Invalid/truncated history must fail startup instead of granting a new
        # budget whose previous use can no longer be established.
        if self.path.exists():
            with self.path.open(encoding="utf-8") as stream:
                for line in stream:
                    previous = json.loads(line)
                    if not isinstance(previous, dict):
                        raise ValueError("Audit history must contain JSON objects")
                    if previous.get("event") == "tool_call_started":
                        if not isinstance(previous.get("budget_admitted"), bool):
                            raise ValueError("Audit start record lacks admission status")
                        self.calls += 1
                        if previous.get("tool") == "build_asset" and previous["budget_admitted"]:
                            self.build_attempts += 1
        self.session_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.current = ContextVar("benchmark_audit_call", default=None)

    def write(self, record):
        record = {"session_id": self.session_id, **record}
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())

    def begin(self, tool, arguments):
        with self.lock:
            self.calls += 1
            rejected = None
            if self.calls > self.max_calls:
                rejected = {"code": "E_BENCHMARK_CALL_BUDGET", "message": "Tool call budget exhausted", "limit": self.max_calls}
            elif tool == "build_asset":
                if self.build_attempts >= self.max_builds:
                    rejected = {"code": "E_BENCHMARK_BUILD_BUDGET", "message": "Build attempt budget exhausted", "limit": self.max_builds}
                else:
                    self.build_attempts += 1
            record = {"tool_call_number": self.calls, "build_attempts": self.build_attempts,
                      "tool": tool, "input": arguments, "started_at": utc_now(),
                      "worker_runs": 0, "budget_admitted": rejected is None}
        self.write({"event": "tool_call_started", **record})
        return record, rejected

    def metadata(self, **values):
        record = self.current.get()
        if record is not None:
            record.setdefault("evidence", {}).update(values)


def _summary(result):
    """Keep result identity and diagnostics, never catalog/schema or image data."""
    from mcp.types import ImageContent, TextContent
    body = result.root
    summary = {}
    error = None
    for item in body.content:
        if isinstance(item, ImageContent):
            summary["native_image"] = True
            summary["mime_type"] = item.mimeType
        elif isinstance(item, TextContent):
            try:
                value = json.loads(item.text)
            except (ValueError, TypeError):
                if body.isError:
                    error = {"code": "E_MCP_INPUT_OR_TOOL", "message": item.text}
                continue
            if not isinstance(value, dict):
                continue
            if value.get("error"):
                error = value["error"]
            for key in ("ok", "asset_id", "request_id", "revision", "cached", "is_latest", "previous_success_revision",
                        "next_build_expected_revision", "freshness", "changes", "diagnostics", "validation", "catalog_sha256"):
                if key in value:
                    summary[key] = value[key]
            if "objects" in value:
                summary["object_count"] = len(value["objects"])
            if "images" in value:
                summary["images"] = [{key: image.get(key) for key in
                    ("artifact_id", "revision", "sha256", "path", "mode", "camera", "pose")} for image in value["images"]]
            if "object" in value:
                summary["object_id"] = value["object"].get("id", value["object"].get("object_id"))
    return not body.isError, error, summary


def make_audited_server(backend_root, output_root, audit_path, asset_id, max_builds=3, max_calls=40):
    backend = load_backend(backend_root)
    from b3dtile.contracts import ContractError, file_sha256
    from b3dtile.service import valid_id
    from mcp.types import CallToolRequest, CallToolResult, ServerResult, TextContent

    valid_id(asset_id)
    audit = Audit(audit_path, asset_id, max_builds, max_calls)
    original_class = backend.AssetService

    class AuditedAssetService(original_class):
        def _check_asset(self, candidate):
            if candidate != asset_id:
                raise ContractError("E_BENCHMARK_ASSET", "Only the assigned benchmark asset is accessible", asset_id=asset_id)

        def get_catalog(self):
            result = super().get_catalog()
            audit.metadata(catalog_sha256=result.get("catalog_sha256"))
            return result

        def execute(self, request):
            if request.get("method") != "get_catalog":
                self._check_asset(request.get("params", {}).get("asset_id"))
            return super().execute(request)

        def build_asset(self, request):
            self._check_asset(request.get("params", {}).get("asset_id"))
            return super().build_asset(request)

        def _run_worker(self, *args, **kwargs):
            record = audit.current.get()
            if record is not None:
                record["worker_runs"] += 1
            return super()._run_worker(*args, **kwargs)

        def observe_asset(self, asset_id, *args, **kwargs):
            self._check_asset(asset_id)
            result = super().observe_asset(asset_id, *args, **kwargs)
            audit.metadata(resolved_revision=result.get("revision"))
            return result

        def inspect_object(self, asset_id, revision, object_id):
            self._check_asset(asset_id)
            return super().inspect_object(asset_id, revision, object_id)

        def artifact_path(self, asset_id, revision, artifact_id, **kwargs):
            self._check_asset(asset_id)
            path = super().artifact_path(asset_id, revision, artifact_id, **kwargs)
            audit.metadata(asset_id=asset_id, revision=revision, artifact_id=artifact_id,
                           sha256=file_sha256(path), path=str(path), bytes=path.stat().st_size)
            return path

    backend.AssetService = AuditedAssetService
    try:
        server = backend.make_server(output_root)
    finally:
        backend.AssetService = original_class
    original_handler = server.request_handlers[CallToolRequest]

    async def audited_handler(request):
        name, arguments = request.params.name, request.params.arguments or {}
        record, rejected = audit.begin(name, arguments)
        token = audit.current.set(record)
        started = time.monotonic()
        try:
            if rejected:
                result = ServerResult(CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps({"ok": False, "error": rejected}))]))
            else:
                result = await original_handler(request)
            ok, error, summary = _summary(result)
            audit.write({"event": "tool_call", **record, "finished_at": utc_now(),
                         "duration_seconds": round(time.monotonic() - started, 6),
                         "ok": ok, "error": error, "result_summary": summary})
            return result
        except BaseException as exc:
            audit.write({"event": "tool_call", **record, "finished_at": utc_now(),
                         "duration_seconds": round(time.monotonic() - started, 6), "ok": False,
                         "error": {"code": "E_AUDIT_HANDLER", "type": type(exc).__name__, "message": str(exc)},
                         "result_summary": {}})
            raise
        finally:
            audit.current.reset(token)

    server.request_handlers[CallToolRequest] = audited_handler
    audit.write({"event": "session_start", "time": utc_now(), "asset_id": asset_id,
                 "backend_root": str(Path(backend_root).resolve()), "output_root": str(Path(output_root).resolve()),
                 "max_builds": max_builds, "max_calls": max_calls,
                 "counting": "Every tools/call counts; admitted build_asset calls consume a build attempt before schema validation; worker_runs counts actual worker launches."})
    return server, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--max-builds", type=int, default=3)
    parser.add_argument("--max-calls", type=int, default=40)
    args = parser.parse_args()
    server, _ = make_audited_server(args.backend_root, args.output_root, args.audit,
                                  args.asset_id, args.max_builds, args.max_calls)
    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main()

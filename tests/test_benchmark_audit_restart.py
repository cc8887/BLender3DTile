"""MCP restarts retain the budget already charged by durable start records."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "benchmark_audit_restart_server", ROOT / "benchmarks" / "audit_server.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Audit = MODULE.Audit


def test_restart_charges_incomplete_build_and_ignores_completion(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = Audit(path, "house_eval", max_builds=2, max_calls=10)
    first.write({"event": "session_start", "asset_id": "house_eval"})
    started, rejected = first.begin("build_asset", {"recipe": "invalid"})
    assert rejected is None
    first.write({"event": "tool_call", **started, "ok": False})
    # No completion record: a crash after dispatch must not refund this attempt.
    first.begin("build_asset", {})

    restarted = Audit(path, "house_eval", max_builds=2, max_calls=10)
    assert restarted.session_id != first.session_id
    assert (restarted.calls, restarted.build_attempts) == (2, 2)
    record, rejected = restarted.begin("build_asset", {})
    assert rejected["code"] == "E_BENCHMARK_BUILD_BUDGET"
    assert (record["tool_call_number"], record["build_attempts"]) == (3, 2)

    again = Audit(path, "house_eval", max_builds=2, max_calls=10)
    assert (again.calls, again.build_attempts) == (3, 2)
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["session_id"] == restarted.session_id
    assert last["budget_admitted"] is False


def test_restart_preserves_call_limit_and_rejected_calls(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = Audit(path, "zombie_eval", max_builds=3, max_calls=2)
    first.begin("get_catalog", {})
    first.begin("unknown_tool", {})

    restarted = Audit(path, "zombie_eval", max_builds=3, max_calls=2)
    record, rejected = restarted.begin("build_asset", {})
    assert rejected["code"] == "E_BENCHMARK_CALL_BUDGET"
    assert (record["tool_call_number"], record["build_attempts"]) == (3, 0)

    again = Audit(path, "zombie_eval", max_builds=3, max_calls=2)
    assert (again.calls, again.build_attempts) == (3, 0)
    assert again.begin("get_catalog", {})[1]["code"] == "E_BENCHMARK_CALL_BUDGET"


def test_aggregate_legacy_sessions_whose_counters_reset(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = [
        {"event": "tool_call_started", "session_id": "old_a", "tool": "get_catalog",
         "tool_call_number": 1, "build_attempts": 0, "budget_admitted": True},
        {"event": "tool_call_started", "session_id": "old_a", "tool": "build_asset",
         "tool_call_number": 2, "build_attempts": 1, "budget_admitted": True},
        {"event": "tool_call_started", "session_id": "old_b", "tool": "build_asset",
         "tool_call_number": 1, "build_attempts": 1, "budget_admitted": True},
        {"event": "tool_call_started", "session_id": "old_b", "tool": "build_asset",
         "tool_call_number": 2, "build_attempts": 1, "budget_admitted": False},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    restarted = Audit(path, "house_eval", max_builds=3, max_calls=10)
    assert (restarted.calls, restarted.build_attempts) == (4, 2)
    record, rejected = restarted.begin("build_asset", {})
    assert rejected is None
    assert (record["tool_call_number"], record["build_attempts"]) == (5, 3)
    assert restarted.begin("build_asset", {})[1]["code"] == "E_BENCHMARK_BUILD_BUDGET"


@pytest.mark.parametrize("history", [
    '{"event":"tool_call_started"',
    '[]\n',
    '{"event":"tool_call_started","tool":"build_asset"}\n',
])
def test_corrupt_history_fails_closed(tmp_path, history):
    path = tmp_path / "audit.jsonl"
    path.write_text(history, encoding="utf-8")
    with pytest.raises(ValueError):
        Audit(path, "house_eval")

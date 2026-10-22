"""Run identical asset briefs in fresh, tool-restricted Codex processes.

Auth stays in the existing Codex installation. No credentials are copied,
printed or included in run artifacts. The frozen backend is shared read-only
by convention; models only receive its five audited MCP tools.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"luna": "gpt-6-luna", "terra": "gpt-5.6-terra", "sol": "gpt-6.1-sol", "astra": "gpt-6-astra"}


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(run_root: Path, baseline: str):
    if run_root.exists():
        raise ValueError("A matrix run requires a fresh output directory")
    commit = subprocess.check_output(["git", "rev-parse", baseline], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=ROOT)
    run_root.mkdir(parents=True)
    backend = run_root / "backend"
    backend.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.infolist():
            if not (backend / member.filename).resolve().is_relative_to(backend.resolve()):
                raise ValueError("Archive entry escapes backend")
        bundle.extractall(backend)
    package = ROOT / ".tools/gltf-validator/node_modules/gltf-validator"
    if not package.is_dir():
        raise ValueError("Official glTF validator must be installed for this benchmark")
    shutil.copytree(package, backend / ".tools/gltf-validator/node_modules/gltf-validator")
    harness = run_root / "harness"
    harness.mkdir()
    for name in ("audit_server.py", "run_matrix.py"):
        shutil.copy2(ROOT / "benchmarks" / name, harness / name)
    # Verify exact committed bytes: source locks must survive Git checkout.
    catalog = json.loads((backend / "catalog/starter_v1.catalog.json").read_text(encoding="utf-8"))
    for name, expected in catalog["source_hashes"].items():
        if sha(backend / "src/b3dtile" / name) != expected:
            raise ValueError(f"Frozen source hash mismatch: {name}")
    prompt_root = run_root / "prompts"
    prompt_root.mkdir()
    for name in ("common", "house", "zombie"):
        shutil.copy2(ROOT / "benchmarks/prompts" / (name + ".md"), prompt_root / (name + ".md"))
    common = (prompt_root / "common.md").read_text(encoding="utf-8")
    for task in ("house", "zombie"):
        (prompt_root / (task + ".prompt.txt")).write_text(common + "\n" + (prompt_root / (task + ".md")).read_text(encoding="utf-8"), encoding="utf-8")
    write_json(run_root / "run.json", {
        "created_at": datetime.now(timezone.utc).isoformat(), "baseline_commit": commit,
        "models": MODELS, "effort": "medium", "trials_per_model_task": 1,
        "runtime": {"python": sys.version, "bpy": version("bpy"), "mcp": version("mcp"),
                    "Pillow": version("Pillow"), "jsonschema": version("jsonschema"),
                    "gltf_validator": json.loads((package / "package.json").read_text())["version"]},
        "harness_sha256": {p.name: sha(p) for p in harness.glob("*.py")},
        "seed": 20261022, "max_build_calls": 3, "max_tool_calls": 40,
        "prompt_sha256": {task: sha(prompt_root / (task + ".prompt.txt")) for task in ("house", "zombie")},
        "provider_cache_state": "unknown; no cache isolation or token-cost attribution claimed",
        "isolation": "8 fresh ephemeral CLI contexts, empty workspaces, independent MCP processes/output roots; shell, web, apps, plugins, skills and subagents disabled",
        "backend_source_fingerprints": {str(p.relative_to(backend)).replace('\\', '/'): sha(p) for p in (backend / "src/b3dtile").glob("*.py")},
    })


def command(codex: str, run_root: Path, run_dir: Path, alias: str, task: str):
    server = {
        "command": sys.executable,
        "args": [str(run_root / "harness/audit_server.py"), "--backend-root", str(run_root / "backend"),
                 "--output-root", str(run_dir / "assets"), "--audit", str(run_dir / "audit.jsonl"),
                 "--asset-id", task + "_eval", "--max-builds", "3", "--max-calls", "40"],
        "startup_timeout_sec": 45, "tool_timeout_sec": 960, "required": True,
    }
    cmd = [codex, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
           "--json", "--color", "never", "--sandbox", "read-only", "--model", MODELS[alias],
           "--cd", str(run_dir / "workspace"), "--output-last-message", str(run_dir / "final.md")]
    overrides = {
        "approval_policy": "never", "model_reasoning_effort": "medium", "web_search": "disabled",
        "suppress_unstable_features_warning": True, "features.shell_snapshot": False,
        "developer_instructions": "This is an isolated asset benchmark. Only use the five Blender3DTile MCP tools for the user's task. Do not use shell, filesystem editing, external services, skills, memory, other agents or other models' results. Do not request unavailable capabilities. Make the requested asset and inspect actual returned images. All build_asset actions within this task are authorized.",
        "features.shell_tool": False, "features.unified_exec": False, "features.apps": False,
        "features.plugins": False, "features.hooks": False, "features.memories": False,
        "features.multi_agent": False, "features.browser_use": False, "features.computer_use": False,
        "features.in_app_browser": False, "features.image_generation": False, "features.view_image": False,
        "features.skill_search": False, "features.skip_host_skill_discovery": True,
        "mcp_servers.blender3dtile.command": server["command"], "mcp_servers.blender3dtile.args": server["args"],
        "mcp_servers.blender3dtile.startup_timeout_sec": 45,
        "mcp_servers.blender3dtile.tool_timeout_sec": 960, "mcp_servers.blender3dtile.required": True,
    }
    for tool in ("get_catalog", "build_asset", "observe_asset", "inspect_object", "read_preview"):
        overrides[f"mcp_servers.blender3dtile.tools.{tool}.approval_mode"] = "auto"
        overrides[f"mcp_servers.blender3dtile.tools.{tool}.output_token_limit"] = 30000
    for key, value in overrides.items():
        cmd.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=False)])
    cmd.append("-")
    return cmd


def run_one(codex, run_root, alias, task, timeout):
    run_dir = run_root / "runs" / (alias + "_" + task)
    if run_dir.exists():
        raise ValueError(f"Refusing to overwrite existing trial: {run_dir.name}")
    (run_dir / "workspace").mkdir(parents=True)
    prompt_path = run_root / "prompts" / (task + ".prompt.txt")
    prompt = prompt_path.read_text(encoding="utf-8")
    cmd = command(codex, run_root, run_dir, alias, task)
    metadata = {"alias": alias, "model": MODELS[alias], "task": task, "status": "running",
                "effort": "medium", "prompt_sha256": sha(prompt_path), "timeout_seconds": timeout,
                "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(run_dir / "run.json", metadata)
    write_json(run_dir / "command.json", cmd)
    started = time.monotonic()
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with (run_dir / "events.jsonl").open("wb") as out, (run_dir / "stderr.log").open("wb") as err:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=run_dir / "workspace",
                                   env={**os.environ, "PYTHONUTF8": "1"}, creationflags=flags)
        metadata["pid"] = process.pid
        write_json(run_dir / "run.json", metadata)
        try:
            process.communicate(prompt.encode("utf-8"), timeout=timeout)
            metadata.update(status="completed" if process.returncode == 0 else "failed", exit_code=process.returncode)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                process.kill()
            process.communicate()
            metadata.update(status="timed_out", exit_code=process.returncode)
    metadata.update(elapsed_seconds=round(time.monotonic() - started, 3), finished_at=datetime.now(timezone.utc).isoformat())
    write_json(run_dir / "run.json", metadata)
    print(json.dumps({"run": run_dir.name, **metadata}), flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--baseline", default="HEAD")
    parser.add_argument("--codex", default=shutil.which("codex.exe"))
    parser.add_argument("--models", nargs="+", choices=list(MODELS), default=list(MODELS))
    parser.add_argument("--tasks", nargs="+", choices=["house", "zombie"], default=["house", "zombie"])
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--prepared", action="store_true", help="Use an already prepared matrix, never overwrite any trial")
    args = parser.parse_args()
    run_root = Path(args.run_root).resolve()
    if not args.prepared:
        prepare(run_root, args.baseline)
    if args.prepare_only:
        print(run_root)
        return
    if not args.codex:
        parser.error("Provide a local Codex executable")
    manifest = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    manifest["cli"] = {"executable": str(Path(args.codex).resolve()),
                       "version": subprocess.check_output([args.codex, "--version"], text=True).strip()}
    write_json(run_root / "run.json", manifest)
    # Dispatch one task per model first, then the second task, with equal concurrency.
    jobs = [(alias, task) for task in args.tasks for alias in args.models]
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = [pool.submit(run_one, args.codex, run_root, alias, task, args.timeout) for alias, task in jobs]
        for future in as_completed(futures):
            future.result()


if __name__ == "__main__":
    main()

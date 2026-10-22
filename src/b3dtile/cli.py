"""Small CLI for the same protocol served over MCP."""
import argparse
import json
from pathlib import Path
import sys
import uuid

from .contracts import ContractError, parse_json, validate_request
from .service import AssetService


def main(argv=None):
    parser = argparse.ArgumentParser(prog="b3dtile")
    parser.add_argument("--output-root", default="out")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    check = sub.add_parser("check")
    check.add_argument("request")
    build = sub.add_parser("build")
    build.add_argument("request")
    build.add_argument("--new-request", action="store_true", help="Use a fresh idempotency ID for an edited recipe")
    build.add_argument("--expected-revision")
    build.add_argument("--backend", choices=("bpy", "cli"), default="bpy")
    build.add_argument("--blender-executable")
    observe = sub.add_parser("observe")
    observe.add_argument("asset_id")
    observe.add_argument("--revision", default="latest")
    observe.add_argument("--known-revision")
    observe.add_argument("--full", action="store_true")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("asset_id")
    inspect.add_argument("revision")
    inspect.add_argument("object_id")
    args = parser.parse_args(argv)
    service = AssetService(args.output_root, backend=getattr(args, "backend", "bpy"), blender_executable=getattr(args, "blender_executable", None))
    try:
        if args.command == "catalog":
            result = {"ok": True, **service.get_catalog()}
        elif args.command in ("check", "build"):
            request = parse_json(Path(args.request).read_text(encoding="utf-8"))
            if args.command == "check":
                plan = validate_request(request)
                result = {"ok": True, "instances": len(plan["objects"]), "warnings": plan["warnings"]}
            else:
                if args.new_request:
                    request["request_id"] = "build_" + uuid.uuid4().hex
                if args.expected_revision:
                    request["expected_revision"] = args.expected_revision
                result = service.execute(request)
        elif args.command == "observe":
            result = {"ok": True, "result": service.observe_asset(args.asset_id, args.revision, args.known_revision, "full" if args.full else "summary")}
        else:
            result = {"ok": True, "result": service.inspect_object(args.asset_id, args.revision, args.object_id)}
    except (ContractError, OSError) as exc:
        result = {"ok": False, "error": exc.diagnostic if isinstance(exc, ContractError) else {"code": "E_IO", "message": str(exc)}}
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Strict JSON, catalog discovery and backend-independent semantic checks."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


class ContractError(ValueError):
    def __init__(self, code: str, message: str, **context):
        super().__init__(message)
        self.diagnostic = {"code": code, "message": message, **context}


def data_path(kind: str, name: str) -> Path:
    checkout = Path(__file__).resolve().parents[2] / kind / name
    if checkout.is_file():
        return checkout
    installed = Path(sys.prefix) / "share" / "b3dtile" / kind / name
    if installed.is_file():
        return installed
    raise FileNotFoundError(f"Missing packaged {kind}/{name}")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("E_JSON_DUPLICATE_KEY", f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ContractError("E_JSON_NONFINITE", f"JSON number must be finite: {value}")


def parse_json(text: str):
    if len(text.encode("utf-8")) > 2_000_000:
        raise ContractError("E_REQUEST_SIZE", "Request exceeds 2 MB")
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except json.JSONDecodeError as exc:
        raise ContractError("E_JSON", str(exc)) from exc


def canonical_bytes(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ContractError("E_JSON", "Request must contain finite JSON values") from exc


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_catalog() -> dict:
    return parse_json(data_path("catalog", "starter_v1.catalog.json").read_text(encoding="utf-8"))


def catalog_digest() -> str:
    return file_sha256(data_path("catalog", "starter_v1.catalog.json"))


def load_schema() -> dict:
    return parse_json(data_path("schemas", "request.schema.json").read_text(encoding="utf-8"))


def implementation_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    digest.update(data_path("schemas", "request.schema.json").read_bytes())
    digest.update(b"bpy=4.5.14;python=3.11;preview=cycles_cpu_v1")
    repo = Path(__file__).resolve().parents[2]
    for extra in (repo / "tools" / "validate_glb.cjs", repo / ".tools" / "gltf-validator" / "node_modules" / "gltf-validator" / "package.json"):
        digest.update(str(extra.name).encode())
        digest.update(extra.read_bytes() if extra.exists() else b"not-installed")
    return digest.hexdigest()


def validate_request(request: dict, catalog: dict | None = None) -> dict:
    # Reject NaN supplied through Python calls as well as JSON text.
    canonical_bytes(request)
    schema = load_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(request), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        leaves = list(error.context)
        if isinstance(request, dict):
            method = request.get("method")
            definition = schema["$defs"].get(method)
            if definition is not None:
                direct = {"$schema": schema["$schema"], "$defs": schema["$defs"], **definition}
                leaves = list(Draft202012Validator(direct).iter_errors(request)) or leaves
        detail = leaves[0] if leaves else error
        raise ContractError("E_SCHEMA", detail.message[:800], path=list(detail.absolute_path))
    catalog = catalog or load_catalog()
    for name, expected_hash in catalog.get("source_hashes", {}).items():
        source = Path(__file__).resolve().with_name(name)
        if file_sha256(source) != expected_hash:
            raise ContractError("E_CATALOG_SOURCE_CHANGED", "Trusted kit implementation changed; regenerate and review the catalog", source=name)
    if request["method"] != "build_asset":
        return {"objects": {}, "warnings": []}
    recipe = request["params"]
    if recipe["catalog_sha256"] != catalog_digest():
        raise ContractError("E_CATALOG_HASH", "Catalog content changed; refresh get_catalog")
    return validate_plan(recipe, catalog)


def validate_plan(recipe: dict, catalog: dict) -> dict:
    kits = {x["id"]: x for x in catalog["kits"]}
    slots = {x["id"]: x for x in catalog["slots"]}
    tiles = {x["id"]: x for x in catalog["tiles"]}
    objects, parents, occupied, bound, painted, assigned = {}, {}, set(), set(), set(), {}
    preview_count = 0
    animated = {}

    def fail(code, message, index, **context):
        raise ContractError(code, message, op_index=index, **context)

    def existing(object_id, index):
        if object_id not in objects:
            fail("E_OBJECT_UNKNOWN", "Object must be placed before use", index, object_id=object_id)
        return objects[object_id]

    for index, op in enumerate(recipe["ops"]):
        kind = op["op"]
        if kind == "place":
            if op["id"] in objects:
                fail("E_OBJECT_DUPLICATE", "Object IDs must be unique", index, object_id=op["id"])
            objects[op["id"]] = op
            if len(objects) > 128:
                fail("E_BUDGET", "At most 128 instances", index)
        elif kind == "paint_tile":
            tile = tiles[op["tile_id"]]
            if tile["id"] in painted:
                fail("E_TILE_ALREADY_PAINTED", "Paint each tile once per recipe", index)
            painted.add(tile["id"])
            if op["pattern"] not in tile["patterns"]:
                fail("E_PATTERN_TILE", "Pattern incompatible with tile", index, allowed=tile["patterns"])
            w, h = tile["rect_px"][2:]
            for mark in op.get("marks", []):
                if mark["x"] >= w - 2 * tile["padding_px"] or mark["y"] >= h - 2 * tile["padding_px"]:
                    fail("E_PIXEL_BOUNDS", "Pixel outside tile inner rectangle", index, tile_id=tile["id"])
        elif kind in ("assign_slot", "set_region_uv", "paint_vertex_color", "set_uv_animation"):
            placed = existing(op["mesh"], index)
            kit = kits[placed["kit_id"]]
            region = kit["regions"].get(op["region"])
            if region is None:
                fail("E_REGION_UNKNOWN", "Region does not belong to this kit", index, object_id=op["mesh"], allowed=list(kit["regions"]))
            key = (op["mesh"], op["region"])
            if kind == "paint_vertex_color":
                continue
            if key in animated:
                fail("E_UV_ANIMATION_LOCKED", "Set slots and UVs before animation; one track per region", index)
            if kind == "set_uv_animation":
                preset = next(p for p in catalog["uv_animation_presets"] if p["id"] == op["preset"])
                slot = assigned.get(key, region["default_slot"])
                if slot != preset["slot"] or not set(preset["frames"]) <= set(slots[slot]["allowed_tiles"]):
                    fail("E_UV_ANIMATION_SLOT", "Flipbook incompatible with region slot", index, required_slot=preset["slot"])
                frame_tiles = [tiles[t] for t in preset["frames"]]
                if len({(t["atlas_id"], *t["rect_px"][2:], t["padding_px"]) for t in frame_tiles}) != 1:
                    fail("E_UV_ANIMATION_LAYOUT", "Frames require equal sizes/padding in one atlas", index)
                animated[key] = op["preset"]
                if len(animated) > catalog["uv_animation_policy"]["max_tracks"]:
                    fail("E_UV_ANIMATION_BUDGET", "At most four animated regions", index)
                continue
            if kind == "assign_slot":
                if op["slot"] not in region["allowed_slots"]:
                    fail("E_SLOT_REGION", "Slot incompatible with region", index, allowed=region["allowed_slots"])
                assigned[key] = op["slot"]
            else:
                slot = assigned.get(key, region["default_slot"])
                if op["tile_id"] not in slots[slot]["allowed_tiles"]:
                    fail("E_REGION_TILE_MISMATCH", "Tile incompatible with assigned slot", index, object_id=op["mesh"], region=op["region"], allowed=slots[slot]["allowed_tiles"])
                if op["orientation"] not in region["allowed_uv_rotations"]:
                    fail("E_UV_ORIENTATION", "UV orientation forbidden for this chart", index, allowed=region["allowed_uv_rotations"])
        elif kind == "attach":
            child, parent = existing(op["child"], index), existing(op["parent"], index)
            ck, pk = kits[child["kit_id"]], kits[parent["kit_id"]]
            if ck["kind"] != "static" or pk["kind"] != "static":
                fail("E_ATTACH_SKIN", "Version 1 sockets are static-only", index)
            cs, ps = ck["sockets"].get(op["child_socket"]), pk["sockets"].get(op["parent_socket"])
            if cs is None or ps is None:
                fail("E_SOCKET_UNKNOWN", "Socket does not exist on the referenced kit", index)
            if cs["type"] != ps["type"] or child["scale"] != parent["scale"]:
                fail("E_SOCKET_FIT", "Socket interface and uniform scales must match", index)
            pair = [f"{ck['id']}:{op['child_socket']}", f"{pk['id']}:{op['parent_socket']}"]
            if pair not in catalog["socket_types"][cs["type"]]["certified_pairs"]:
                fail("E_SOCKET_PAIR", "Uncertified socket pair", index)
            if op["child"] in parents:
                fail("E_ALREADY_ATTACHED", "Child already attached", index)
            for key in ((op["child"], op["child_socket"]), (op["parent"], op["parent_socket"])):
                if key in occupied:
                    fail("E_SOCKET_OCCUPIED", "Socket is occupied", index)
            cursor = op["parent"]
            while True:
                if cursor == op["child"]:
                    fail("E_ATTACH_CYCLE", "Socket graph would contain a cycle", index)
                if cursor not in parents:
                    break
                cursor = parents[cursor]
            occupied.update(((op["child"], op["child_socket"]), (op["parent"], op["parent_socket"])))
            parents[op["child"]] = op["parent"]
        elif kind == "bind_humanoid":
            placed = existing(op["mesh"], index)
            kit = kits[placed["kit_id"]]
            if op["preset"] not in kit.get("rig_presets", [kit.get("rig_preset")]):
                fail("E_RIG_KIT", "Rig preset requires a compatible humanoid kit", index)
            if op["mesh"] in bound:
                fail("E_ALREADY_BOUND", "Object already bound", index)
            bound.add(op["mesh"])
        elif kind == "render_preview":
            preview_count += 1
            if preview_count > 12:
                fail("E_PREVIEW_BUDGET", "At most 12 requested previews", index)
            if op["pose"] != "rest" and not bound:
                fail("E_POSE_UNBOUND", "Pose preview requires an already-bound character", index)
        elif kind == "validate":
            expected = "character_v1" if recipe["asset_kind"] == "character" else "prop_v1"
            if op["profile"] != expected:
                fail("E_PROFILE", "Validation profile does not match asset kind", index, expected=expected)
            if expected == "character_v1" and not bound:
                fail("E_RIG_MISSING", "Character validation requires binding first", index)
        elif kind == "export_glb":
            if index != len(recipe["ops"]) - 1:
                fail("E_EXPORT_ORDER", "export_glb must be last", index)
            if op["clips"] != "none" and not bound:
                fail("E_CLIP_UNBOUND", "Animation export requires a bound character", index)

    humanoids = [oid for oid, op in objects.items() if kits[op["kit_id"]]["kind"] == "skinned"]
    if not objects:
        raise ContractError("E_EMPTY_ASSET", "Asset needs at least one mesh")
    if recipe["asset_kind"] == "prop" and humanoids:
        raise ContractError("E_ASSET_KIND", "Prop assets cannot contain humanoid kits")
    if recipe["asset_kind"] == "character" and len(humanoids) != 1:
        raise ContractError("E_ASSET_KIND", "Character assets require exactly one humanoid")
    if set(humanoids) != bound:
        raise ContractError("E_RIG_MISSING", "Every humanoid must be bound before export")
    return {"objects": objects, "parents": parents, "bound": sorted(bound), "warnings": []}

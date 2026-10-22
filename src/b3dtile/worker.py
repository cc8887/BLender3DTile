"""Trusted Blender interpreter. Only the host launches this module."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

from .contracts import ContractError, implementation_digest, load_catalog, parse_json, validate_request
from .service import write_json


def _geometry_checks(instances):
    errors, metrics = [], {}
    total_triangles = 0
    for object_id, instance in instances.items():
        mesh = instance.mesh.data
        mesh.calc_loop_triangles()
        finite = all(math.isfinite(c) for v in mesh.vertices for c in v.co)
        covered = [i for values in instance.regions.values() for i in values]
        degenerate = sum(t.area < 1e-12 for t in mesh.loop_triangles)
        if not finite:
            errors.append({"code": "E_GEOMETRY_NONFINITE", "object_id": object_id})
        if sorted(covered) != list(range(len(mesh.polygons))):
            errors.append({"code": "E_REGION_COVERAGE", "object_id": object_id})
        if len(instance.uv_template) != len(mesh.loops):
            errors.append({"code": "E_UV_TEMPLATE", "object_id": object_id})
        if degenerate:
            errors.append({"code": "E_DEGENERATE_TRIANGLES", "object_id": object_id, "count": degenerate})
        metrics[object_id] = {"vertices": len(mesh.vertices), "triangles": len(mesh.loop_triangles),
                              "regions": len(instance.regions), "degenerate_triangles": degenerate,
                              "topology_sha256": instance.meta.get("topology_sha256")}
        total_triangles += len(mesh.loop_triangles)
    if total_triangles > 100_000:
        errors.append({"code": "E_TRIANGLE_BUDGET", "actual": total_triangles, "maximum": 100_000})
    return {"ok": not errors, "errors": errors, "metrics": metrics, "triangles": total_triangles}


def _attach(child, parent, child_socket, parent_socket, catalog):
    import bpy
    from mathutils import Euler, Matrix, Vector
    kits = {k["id"]: k for k in catalog["kits"]}
    def socket_frame(spec):
        return Matrix.Translation(Vector(spec["pos_m"])) @ Euler(tuple(math.radians(v) for v in spec["rot_xyz_deg"]), "XYZ").to_matrix().to_4x4()
    bpy.context.view_layer.update()
    world = parent.root.matrix_world @ socket_frame(kits[parent.kit_id]["sockets"][parent_socket]) @ socket_frame(kits[child.kit_id]["sockets"][child_socket]).inverted()
    child.root.parent = parent.root
    child.root.matrix_world = world
    bpy.context.view_layer.update()
    child.meta["attachment"] = {"parent": parent.object_id, "parent_socket": parent_socket, "child_socket": child_socket}


def _official_validator(path):
    script = Path(__file__).resolve().parents[2] / "tools" / "validate_glb.cjs"
    package = script.parent.parent / ".tools" / "gltf-validator" / "node_modules" / "gltf-validator"
    node = shutil.which("node")
    if not node or not script.is_file() or not package.is_dir():
        return {"status": "not_installed", "ok": None, "note": "Run tools/bootstrap.ps1 to enable the official Khronos validator."}
    result = subprocess.run([node, str(script), str(path)], capture_output=True, text=True, encoding="utf-8", timeout=120)
    if result.returncode:
        return {"status": "failed", "ok": False, "error": result.stderr[-2000:]}
    report = json.loads(result.stdout)
    return {"status": "completed", "ok": report["issues"]["numErrors"] == 0,
            "errors": report["issues"]["numErrors"], "warnings": report["issues"]["numWarnings"],
            "messages": report["issues"]["messages"], "validator_version": report.get("validatorVersion")}


def build(request, output: Path, revision: str):
    import bpy
    from .atlas import build_atlases, build_debug_atlases
    from .geometry import build_kit
    from .materials import AtlasMaterials
    from .rig import bind_humanoid, create_smoke_animation, set_pose, validate_rig
    from .glb import export_glb
    from .observation import capture_observation

    if tuple(bpy.app.version) != (4, 5, 14):
        raise ContractError("E_BACKEND_VERSION", "This catalog requires Blender/bpy 4.5.14", actual=bpy.app.version_string)
    catalog = load_catalog()
    validate_request(request, catalog)
    recipe = request["params"]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.unit_settings.system = "METRIC"
    bpy.context.scene.unit_settings.scale_length = 1.0
    atlas_paths = build_atlases(catalog, recipe, output / "textures")
    build_debug_atlases(catalog, output / "textures" / "debug")
    materials = AtlasMaterials(catalog, atlas_paths)
    instances = {}
    for index, op in enumerate(recipe["ops"]):
        try:
            kind = op["op"]
            if kind == "place":
                inst = build_kit(op["kit_id"], op["id"], catalog)
                inst.root.location = op["pos"]
                inst.root.rotation_euler = (0, 0, math.radians(int(op["rot"].split("_")[1])))
                inst.root.scale = (op["scale"],) * 3
                inst.root["object_id"] = op["id"]
                inst.mesh["object_id"] = op["id"]
                inst.mesh["kit_id"] = op["kit_id"]
                materials.apply_defaults(inst)
                instances[op["id"]] = inst
            elif kind == "attach":
                _attach(instances[op["child"]], instances[op["parent"]], op["child_socket"], op["parent_socket"], catalog)
            elif kind == "assign_slot":
                materials.assign_slot(instances[op["mesh"]], op["region"], op["slot"])
            elif kind == "set_region_uv":
                materials.set_region_uv(instances[op["mesh"]], op["region"], op["tile_id"], op["orientation"])
            elif kind == "bind_humanoid":
                bind_humanoid(instances[op["mesh"]], op["preset"], catalog)
            # paint_tile is initialized deterministically before placement;
            # validate/render_preview declare checks/views of the final frozen scene.
        except Exception as exc:
            if isinstance(exc, ContractError):
                raise
            raise ContractError("E_OPERATION", str(exc), op_index=index, op=op["op"]) from exc

    bpy.context.view_layer.update()
    validation = {"geometry": _geometry_checks(instances), "uv": {}, "rig": {}}
    for oid, inst in instances.items():
        problems = materials.diagnostics(inst)
        validation["uv"][oid] = {"ok": not problems, "errors": problems}
        if inst.rig is not None:
            validation["rig"][oid] = validate_rig(inst)
    failures = []
    if not validation["geometry"]["ok"]:
        failures.extend(validation["geometry"]["errors"])
    for group in ("uv", "rig"):
        for oid, report in validation[group].items():
            if not report["ok"]:
                failures.append({"object_id": oid, "category": group, "report": report})
    write_json(output / "validation.json", validation)
    if failures:
        raise ContractError("E_ASSET_VALIDATION", "Asset failed pre-export checks", failures=failures)

    # Freeze the visual snapshot before creating an animation action. Preview poses
    # are temporary; the rig and mesh must remain in canonical rest state.
    observation = capture_observation(instances, recipe, catalog, output, revision, validation)
    for inst in instances.values():
        set_pose(inst, "rest")
    include_animation = recipe["ops"][-1]["clips"] != "none"
    if include_animation:
        for inst in instances.values():
            if inst.rig is not None:
                create_smoke_animation(inst)
    glb_path = output / (recipe["asset_id"] + ".glb")
    validation["glb"] = export_glb(instances, glb_path, include_animation)
    # This packet moves from staging into an immutable revision directory.
    # Keep its references relative so no published diagnostic points at staging.
    validation["glb"]["path"] = glb_path.name
    validation["khronos"] = _official_validator(glb_path)
    write_json(output / "validation.json", validation)
    if not validation["glb"].get("ok") or validation["khronos"].get("ok") is False:
        raise ContractError("E_GLB_VALIDATION", "Exported GLB failed validation", reports={"glb": validation["glb"], "khronos": validation["khronos"]})
    # Persist provenance, authored regions and weight tables for diagnosis without
    # exposing any mesh/UV edits through the public protocol.
    write_json(output / "model_data.json", {oid: {"kit_id": i.kit_id, "meta": i.meta, "regions": i.regions,
               "region_slots": i.region_slots, "region_tiles": i.region_tiles} for oid, i in instances.items()})
    bpy.ops.wm.save_as_mainfile(filepath=str(output / "debug.blend"))
    observation["diagnostics"] = validation
    observation["glb"] = glb_path.name
    write_json(output / "observation.json", observation)
    return {"ok": True, "runtime": {"blender": bpy.app.version_string, "python": sys.version.split()[0]}, "validation": validation}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--implementation-sha256", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        if implementation_digest() != args.implementation_sha256:
            raise ContractError("E_SOURCE_CHANGED", "Worker source differs from host snapshot")
        result = build(parse_json(Path(args.request).read_text(encoding="utf-8")), output, args.revision)
        code = 0
    except Exception as exc:
        traceback.print_exc()
        diagnostic = exc.diagnostic if isinstance(exc, ContractError) else {"code": "E_WORKER", "message": str(exc)}
        result = {"ok": False, "error": diagnostic}
        code = 1
    write_json(output / "worker_result.json", result)
    return code


if __name__ == "__main__":
    from .lifecycle import finish_process
    finish_process(main())

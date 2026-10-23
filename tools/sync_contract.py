"""Publish the built-in catalog and refresh schemas/example content locks."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from b3dtile.rig_presets import catalog_presets, RIG_IDS, WEIGHT_IDS


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def obj(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


catalog_path = ROOT / "catalog/starter_v1.catalog.json"
catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
catalog["catalog_version"] = "starter_v1"
catalog["status"] = "implemented_procedural_templates"
catalog["runtime_policy"]["blender_baseline"] = "bpy/Blender 4.5.14, Python 3.11"
catalog["source_hashes"] = {name: hashlib.sha256((ROOT / "src/b3dtile" / name).read_bytes()).hexdigest()
                            for name in ("geometry.py", "rig.py", "rig_presets.py", "atlas.py", "materials.py", "vertex_colors.py", "uv_animation.py")}
for kit in catalog["kits"]:
    kit["status"] = "implemented"
    kit["source"] = "builtin:b3dtile.geometry.build_kit/" + kit["id"]
    if kit["id"] == "humanoid_round_v1":
        kit["rig_presets"] = list(RIG_IDS)
        kit["topology_sha256"] = "36abec0cf45b2ba9a7eca8fd4e384f3e3a93ea8813cfebcd33fc8321c0ea915f"
        kit["description"] = "Connected manifold rounded humanoid; fixed Skin cage and baked subdivision; 436 vertices, 434 quads; continuous region charts; A rest pose"
catalog["rig_presets"] = catalog_presets()
catalog["weight_presets"] = [{"id": name, "topology_locked": True, "max_influences": 4,
                               "description": "Deterministic normalized weights; crisp uses exponent 1.35 after merging bones"}
                              for name in WEIGHT_IDS]
catalog["vertex_color_policy"] = {"styles": ["solid", "height_gradient"], "blend": "multiply_atlas",
    "domain": "CORNER", "export": "COLOR_0 linear RGBA", "unpainted": "white", "alpha": 1}
for tile_id, x, color in [("env_glass_amber", 192, 10), ("env_glass_dark", 224, 0)]:
    if not any(t["id"] == tile_id for t in catalog["tiles"]):
        catalog["tiles"].append({"id": tile_id, "atlas_id": "environment_v1", "slot": "glass", "rect_px": [x, 0, 32, 32],
            "padding_px": 2, "patterns": ["solid"], "default_pattern": "solid", "default_palette": "pastel16", "base_color_index": color})
glass = next(s for s in catalog["slots"] if s["id"] == "glass")
glass["allowed_tiles"] = ["env_glass_blue", "env_glass_amber", "env_glass_dark"]
catalog["uv_animation_presets"] = [
    {"id": "signal_blink_v1", "slot": "glass", "frames": ["env_glass_blue", "env_glass_dark"], "fps": [1, 2, 4, 8]},
    {"id": "signal_cycle_v1", "slot": "glass", "frames": ["env_glass_blue", "env_glass_amber", "env_glass_dark", "env_glass_amber"], "fps": [1, 2, 4, 8]}]
catalog["uv_animation_policy"] = {"max_tracks": 4, "interpolation": "STEP", "loop": True,
    "export": ["KHR_animation_pointer", "KHR_texture_transform"], "fallback": "static first frame",
    "sidecar": "uv_animation.json", "scope": "enumerated same-size atlas flipbooks; no arbitrary UV paths"}
catalog["authoring_requirements"]["hashes"] = "Built-in procedural sources are pinned by source_hashes; every build also hashes all runtime source. No external .blend file is needed for these eight kits."
catalog["authoring_requirements"]["kit_files"] = ["catalog manifest", "trusted procedural generator", "region/UV templates embedded in generator"]
catalog["authoring_requirements"]["humanoid_extra_files"] = ["fixed bone coordinates and weight rules in rig.py/geometry.py", "catalog topology_sha256", "model_data.json per build"]
catalog["observation_policy"] = {"snapshot": "final frozen scene, never an intermediate command state",
    "publication": "immutable content revision; latest pointer commits only after GLB and all images pass checks",
    "reads": "observe_asset resolves latest once; read_preview and inspect_object require that explicit revision",
    "views": ["beauty", "clay", "wireframe", "object_id", "uv_debug", "vertex_color", "uv_animation_frames", "atlas_uv", "pose", "contact_sheet"],
    "failure": "previous successful revision is retained, with latest accepted build attempt status reported separately",
    "external_edits": "External Blender UI/.blend edits are not watched; rebuild a recipe to publish a new observation."}
write(catalog_path, catalog)
digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
schema_path = ROOT / "schemas/request.schema.json"
schema = json.loads(schema_path.read_text(encoding="utf-8"))
schema["$id"] = "urn:blender3dtile:request:0.3.0"
schema["title"] = "Blender3DTile constrained construction and revision-bound observation API"
defs = schema["$defs"]
defs["recipe"]["properties"]["schema_version"] = {"const": "0.3.0"}
defs["recipe"]["properties"]["catalog_version"] = {"const": "starter_v1"}
defs["recipe"]["properties"]["catalog_sha256"] = {"const": digest}
defs["recipe"]["description"] = "A complete declarative build. Schema and runtime semantic validation are both required. Previews and checks refer to the final frozen scene."
defs["get_catalog"]["properties"]["params"]["properties"]["catalog_version"] = {"const": "starter_v1"}
revision = {"type": "string", "pattern": "^[0-9a-f]{16}$"}
defs["build_asset"]["properties"]["expected_revision"] = {"anyOf": [revision, {"type": "null"}]}
id_ref = {"$ref": "#/$defs/id"}
defs["tile_id"]["enum"] = [t["id"] for t in catalog["tiles"]]
defs["bind_humanoid"]["properties"]["preset"] = {"enum": list(RIG_IDS)}
defs["bind_humanoid"]["properties"]["weight_preset"] = {"enum": list(WEIGHT_IDS), "default": WEIGHT_IDS[0]}
defs["paint_vertex_color"] = obj({"op": {"const": "paint_vertex_color"}, "mesh": id_ref,
    "region": {"$ref": "#/$defs/region_id"}, "style": {"enum": ["solid", "height_gradient"]},
    "palette": {"enum": list(catalog["palettes"])}, "color_a": {"type": "integer", "minimum": 0, "maximum": 15},
    "color_b": {"type": "integer", "minimum": 0, "maximum": 15}})
defs["set_uv_animation"] = obj({"op": {"const": "set_uv_animation"}, "mesh": id_ref,
    "region": {"$ref": "#/$defs/region_id"}, "preset": {"enum": [p["id"] for p in catalog["uv_animation_presets"]]},
    "fps": {"enum": [1, 2, 4, 8]}})
for op in ("paint_vertex_color", "set_uv_animation"):
    ref = {"$ref": "#/$defs/" + op}
    if ref not in defs["command"]["oneOf"]:
        defs["command"]["oneOf"].append(ref)
observe = obj({"asset_id": id_ref, "revision": {"anyOf": [revision, {"const": "latest"}]},
               "known_revision": {"anyOf": [revision, {"type": "null"}]}, "detail": {"enum": ["summary", "full"]}}, ["asset_id"])
inspect = obj({"asset_id": id_ref, "revision": revision, "object_id": id_ref})
preview = obj({"asset_id": id_ref, "revision": revision, "artifact_id": {"type": "string", "pattern": "^a_[0-9a-f]{12}$"}})
for name, params in (("observe_asset", observe), ("inspect_object", inspect), ("read_preview", preview)):
    defs[name] = obj({"method": {"const": name}, "request_id": id_ref, "params": params})
schema["oneOf"] = [{"$ref": "#/$defs/" + name} for name in ("get_catalog", "build_asset", "observe_asset", "inspect_object", "read_preview")]
write(schema_path, schema)
for path in (ROOT / "examples").glob("*.request.json"):
    request = json.loads(path.read_text(encoding="utf-8"))
    request["params"].update(schema_version="0.3.0", catalog_version="starter_v1", catalog_sha256=digest)
    write(path, request)
print("Published starter_v1:", digest)

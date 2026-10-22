"""Publish the built-in catalog and refresh schemas/example content locks."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
                            for name in ("geometry.py", "rig.py", "atlas.py", "materials.py")}
for kit in catalog["kits"]:
    kit["status"] = "implemented"
    kit["source"] = "builtin:b3dtile.geometry.build_kit/" + kit["id"]
    if kit["id"] == "humanoid_round_v1":
        kit["topology_sha256"] = "36abec0cf45b2ba9a7eca8fd4e384f3e3a93ea8813cfebcd33fc8321c0ea915f"
        kit["description"] = "Connected manifold rounded humanoid; fixed Skin cage and baked subdivision; 436 vertices, 434 quads; continuous region charts; A rest pose"
catalog["rig_presets"][0]["weight_method"] = "Versioned anatomical envelopes deterministically generate a normalized vertex-ID weight table; topology checksum is verified before binding. Table is saved in model_data.json."
catalog["authoring_requirements"]["hashes"] = "Built-in procedural sources are pinned by source_hashes; every build also hashes all runtime source. No external .blend file is needed for these eight kits."
catalog["authoring_requirements"]["kit_files"] = ["catalog manifest", "trusted procedural generator", "region/UV templates embedded in generator"]
catalog["authoring_requirements"]["humanoid_extra_files"] = ["fixed bone coordinates and weight rules in rig.py/geometry.py", "catalog topology_sha256", "model_data.json per build"]
catalog["observation_policy"] = {"snapshot": "final frozen scene, never an intermediate command state",
    "publication": "immutable content revision; latest pointer commits only after GLB and all images pass checks",
    "reads": "observe_asset resolves latest once; read_preview and inspect_object require that explicit revision",
    "views": ["beauty", "clay", "wireframe", "object_id", "uv_debug", "atlas_uv", "pose", "contact_sheet"],
    "failure": "previous successful revision is retained, with latest accepted build attempt status reported separately",
    "external_edits": "External Blender UI/.blend edits are not watched; rebuild a recipe to publish a new observation."}
write(catalog_path, catalog)
digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
schema_path = ROOT / "schemas/request.schema.json"
schema = json.loads(schema_path.read_text(encoding="utf-8"))
schema["$id"] = "urn:blender3dtile:request:0.2.0"
schema["title"] = "Blender3DTile constrained construction and revision-bound observation API"
defs = schema["$defs"]
defs["recipe"]["properties"]["schema_version"] = {"const": "0.2.0"}
defs["recipe"]["properties"]["catalog_version"] = {"const": "starter_v1"}
defs["recipe"]["properties"]["catalog_sha256"] = {"const": digest}
defs["recipe"]["description"] = "A complete declarative build. Schema and runtime semantic validation are both required. Previews and checks refer to the final frozen scene."
defs["get_catalog"]["properties"]["params"]["properties"]["catalog_version"] = {"const": "starter_v1"}
revision = {"type": "string", "pattern": "^[0-9a-f]{16}$"}
defs["build_asset"]["properties"]["expected_revision"] = {"anyOf": [revision, {"type": "null"}]}
id_ref = {"$ref": "#/$defs/id"}
observe = obj({"asset_id": id_ref, "revision": {"anyOf": [revision, {"const": "latest"}]},
               "known_revision": {"anyOf": [revision, {"type": "null"}]}, "detail": {"enum": ["summary", "full"]}}, ["asset_id"])
inspect = obj({"asset_id": id_ref, "revision": revision, "object_id": id_ref})
preview = obj({"asset_id": id_ref, "revision": revision, "artifact_id": {"type": "string", "pattern": "^a_[0-9a-f]{12}$"}})
for name, params in (("observe_asset", observe), ("inspect_object", inspect), ("read_preview", preview)):
    defs[name] = obj({"method": {"const": name}, "request_id": id_ref, "params": params})
schema["oneOf"] = [{"$ref": "#/$defs/" + name} for name in ("get_catalog", "build_asset", "observe_asset", "inspect_object", "read_preview")]
write(schema_path, schema)
for filename in ("house.request.json", "zombie.request.json"):
    path = ROOT / "examples" / filename
    request = json.loads(path.read_text(encoding="utf-8"))
    request["params"].update(schema_version="0.2.0", catalog_version="starter_v1", catalog_sha256=digest)
    write(path, request)
print("Published starter_v1:", digest)

"""Offline authoring intake: extract GLB skeletons/weights/colors/UVs for review.

Accepts embedded GLB data only. Does not execute Blender, import scripts, infer
rig compatibility, transfer weights, or publish a runtime kit. The source GLB
must be retained for its textures/material resources. No model service API
exposes this command; a developer must certify and register extracted assets.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from b3dtile.glb import _read_glb, _Binary, _world_matrices


def extract(source, source_label="unspecified", license_note="not provided"):
    source = Path(source)
    document, binary = _read_glb(source)
    if any("uri" in buffer for buffer in document.get("buffers", [])):
        raise ValueError("External buffers are not supported; export an embedded GLB")
    if any("uri" in image for image in document.get("images", [])):
        raise ValueError("External images are not supported; export an embedded GLB")
    reader = _Binary(document, binary)
    worlds = _world_matrices(document.get("nodes", []))
    parents = {child: parent for parent, node in enumerate(document.get("nodes", [])) for child in node.get("children", [])}
    skins = []
    for index, skin in enumerate(document.get("skins", [])):
        joints = skin["joints"]
        inverse = reader.accessor(skin["inverseBindMatrices"]) if "inverseBindMatrices" in skin else None
        if inverse is not None and len(inverse) != len(joints):
            raise ValueError("Inverse bind matrix count does not match joints")
        skins.append({"source_skin": index, "skeleton_node": skin.get("skeleton"),
                      "joints": [{"source_node": node, "name": document["nodes"][node].get("name"),
                                  "parent_node": parents.get(node), "world_matrix_rows": worlds[node]}
                                 for node in joints], "inverse_bind_matrices_gltf_column_major": inverse})
    meshes = []
    notes = ["Uncertified authoring data. Preserve the source GLB; textures are not duplicated in this JSON.",
             "Indices are source primitive-local exported vertices, not Blender vertex IDs. Do not apply weights to another topology."]
    for index, mesh in enumerate(document.get("meshes", [])):
        primitives = []
        for primitive in mesh.get("primitives", []):
            attributes = {name: reader.accessor(accessor) for name, accessor in primitive["attributes"].items()
                          if name == "POSITION" or name == "NORMAL" or name.startswith(("TEXCOORD_", "COLOR_", "JOINTS_", "WEIGHTS_"))}
            count = len(attributes["POSITION"])
            if any(len(values) != count for values in attributes.values()):
                raise ValueError("Attribute counts do not match POSITION")
            if any(not math.isfinite(value) for values in attributes.values() for row in values for value in row):
                raise ValueError("Nonfinite source attribute")
            if any(value < 0 or value > 1 for name, rows in attributes.items() if name.startswith("TEXCOORD_") for row in rows for value in row):
                notes.append("UV values outside [0,1]: possible tiling or encoded UV animation; requires explicit conversion, never automatic clamping.")
            indices = reader.accessor(primitive["indices"]) if "indices" in primitive else [[i] for i in range(count)]
            if any(len(row) != 1 or type(row[0]) is not int or not 0 <= row[0] < count for row in indices):
                raise ValueError("Invalid source triangle index")
            primitives.append({"mode": primitive.get("mode", 4), "material": primitive.get("material"),
                               "attributes": attributes, "indices": indices})
        topology = [{"positions": p["attributes"]["POSITION"], "indices": p["indices"], "mode": p["mode"]} for p in primitives]
        meshes.append({"source_mesh": index, "name": mesh.get("name"), "primitives": primitives,
                       "source_topology_sha256": hashlib.sha256(json.dumps(topology, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
    animations = [{"name": animation.get("name"), "channels": animation["channels"],
                   "samplers": [{"times": reader.accessor(s["input"]), "values": reader.accessor(s["output"]),
                                 "interpolation": s.get("interpolation", "LINEAR")} for s in animation["samplers"]]}
                  for animation in document.get("animations", [])]
    return {"format": "b3dtile_source_review_v1", "certified": False,
            "source": {"filename": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                       "label": source_label, "license_note": license_note, "generator": document.get("asset", {}).get("generator")},
            "coordinates": "source glTF right-handed +Y up; do not assume Blender +Z up or a shared bind pose",
            "nodes": document.get("nodes", []), "skins": skins, "meshes": meshes, "animations": animations,
            "materials": document.get("materials", []), "extensions_used": document.get("extensionsUsed", []),
            "notes": list(dict.fromkeys(notes))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-label", default="unspecified")
    parser.add_argument("--license-note", default="not provided")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise ValueError("Output must not overwrite the source GLB")
    result = extract(args.input, args.source_label, args.license_note)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "skins": len(result["skins"]), "meshes": len(result["meshes"]), "certified": False}))


if __name__ == "__main__":
    main()

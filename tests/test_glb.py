"""Independent binary inspection regressions plus real Blender export smoke."""
from io import BytesIO
import json
import math
from pathlib import Path
import struct

from PIL import Image
import pytest

from b3dtile.glb import _Binary, _read_glb, _write_glb, inspect_glb


ROOT = Path(__file__).resolve().parents[1]
BONES = ["root", "pelvis", "spine", "chest", "neck", "head"] + [
    f"{part}_{side}" for side in ("l", "r") for part in ("clavicle", "upper_arm", "forearm", "hand", "thigh", "shin", "foot")]


def make_glb(path, *, skinned=False, animation=False):
    document = {"asset": {"version": "2.0"}, "bufferViews": [], "accessors": [], "buffers": [],
                "samplers": [{"magFilter": 9728, "minFilter": 9728, "wrapS": 33071, "wrapT": 33071}],
                "textures": [{"source": 0, "sampler": 0}],
                "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
                "nodes": [{"mesh": 0}], "scene": 0, "scenes": [{"nodes": [0]}]}
    binary = bytearray()
    def view(blob):
        binary.extend(b"\0" * (-len(binary) % 4))
        result = len(document["bufferViews"])
        document["bufferViews"].append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(blob)})
        binary.extend(blob)
        return result
    def accessor(rows, kind, component=5126):
        code = {5126: "f", 5121: "B", 5123: "H"}[component]
        values = [v for row in rows for v in row]
        blob = struct.pack("<" + code * len(values), *values)
        index = len(document["accessors"])
        document["accessors"].append({"bufferView": view(blob), "componentType": component, "count": len(rows), "type": kind})
        return index
    im = Image.new("RGB", (2, 2), (100, 150, 200))
    encoded = BytesIO()
    im.save(encoded, format="PNG")
    document["images"] = [{"mimeType": "image/png", "bufferView": view(encoded.getvalue())}]
    positions = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0)]
    attributes = {"POSITION": accessor(positions, "VEC3"),
                  "TEXCOORD_0": accessor([(.1, .1), (.9, .1), (.1, .9), (.9, .9)], "VEC2")}
    if skinned:
        attributes["JOINTS_0"] = accessor([(0, 0, 0, 0)] * 4, "VEC4", 5121)
        attributes["WEIGHTS_0"] = accessor([(1, 0, 0, 0)] * 4, "VEC4")
        identity = tuple(1. if row == col else 0. for col in range(4) for row in range(4))
        bind = accessor([identity] * 20, "MAT4")
        document["skins"] = [{"joints": list(range(1, 21)), "inverseBindMatrices": bind}]
        document["nodes"][0]["skin"] = 0
        document["nodes"].extend({"name": name} for name in BONES)
        document["scenes"][0]["nodes"].extend(range(1, 21))
    if animation:
        times = accessor([(0,), (1,)], "SCALAR")
        values = accessor([(0, 0, 0), (0, 1, 0)], "VEC3")
        document["animations"] = [{"samplers": [{"input": times, "output": values, "interpolation": "LINEAR"}],
                                  "channels": [{"sampler": 0, "target": {"node": 1, "path": "translation"}}]}]
    document["meshes"] = [{"primitives": [{"attributes": attributes, "indices": accessor([(0,), (1,), (2,), (3,), (1,), (2,)], "SCALAR", 5123), "material": 0}]}]
    document["buffers"] = [{"byteLength": len(binary)}]
    _write_glb(path, document, bytes(binary))
    return document, bytes(binary)


def alter_accessor(document, binary, index, values):
    spec = document["accessors"][index]
    view = document["bufferViews"][spec["bufferView"]]
    mutable = bytearray(binary)
    struct.pack_into("<" + "f" * len(values), mutable, view.get("byteOffset", 0) + spec.get("byteOffset", 0), *values)
    return bytes(mutable)


def test_valid_static_and_sampled_skin(tmp_path):
    path = tmp_path / "sample.glb"
    make_glb(path)
    assert inspect_glb(path)["ok"]
    missing = inspect_glb(path, require_skin=True, require_animation=True)
    assert {e["code"] for e in missing["errors"]} >= {"E_GLB_SKIN_MISSING", "E_GLB_ANIMATION_MISSING"}
    make_glb(path, skinned=True, animation=True)
    result = inspect_glb(path, require_skin=True, require_animation=True)
    assert result["ok"], result
    assert result["metrics"]["seam_duplicate_vertices"] == 1
    assert result["metrics"]["max_rest_error_m"] == 0
    assert result["metrics"]["animation_max_displacement_m"] == 1
    assert result["metrics"]["sampled_skin_poses"] == 3


def test_skin_ignores_mesh_node_and_ancestor_transforms_in_world_bind_contract(tmp_path):
    path=tmp_path/"skin_mesh_transform.glb"
    doc,binary=make_glb(path,skinned=True,animation=True)
    doc["nodes"][0].update(translation=[1.25,.5,2],
        rotation=[0,math.sin(math.pi/4),0,math.cos(math.pi/4)],scale=[2,2,2])
    holder=len(doc["nodes"])
    doc["nodes"].append({"name":"mesh_only_parent","translation":[4,3,2],"children":[0]})
    doc["scenes"][0]["nodes"]=[holder]+list(range(1,21))
    _write_glb(path,doc,binary)
    # Joint worlds and inverse binds determine skinned world positions. The
    # mesh node's complete global transform is ignored, including ancestors.
    # POSITION is already in the builder's declared world bind coordinate
    # system; applying mesh-world here would introduce a second transform.
    report=inspect_glb(path,require_skin=True,require_animation=True)
    assert report["ok"],report
    assert report["metrics"]["max_rest_error_m"]==0
    assert report["metrics"]["animation_max_displacement_m"]==1


def test_detects_bad_weights_from_binary_and_not_just_fields(tmp_path):
    path = tmp_path / "weights.glb"
    doc, binary = make_glb(path, skinned=True)
    weight_index = doc["meshes"][0]["primitives"][0]["attributes"]["WEIGHTS_0"]
    binary = alter_accessor(doc, binary, weight_index, [.4, .1, 0, 0])
    _write_glb(path, doc, binary)
    result = inspect_glb(path, require_skin=True)
    assert not result["ok"]
    assert "E_GLB_WEIGHT_SUM" in {e["code"] for e in result["errors"]}


def test_detects_incorrect_bind_and_seam_weights(tmp_path):
    path = tmp_path / "bind.glb"
    doc, binary = make_glb(path, skinned=True)
    bind = doc["skins"][0]["inverseBindMatrices"]
    translated = [1., 0, 0, 0, 0, 1., 0, 0, 0, 0, 1., 0, .1, 0, 0, 1.]
    _write_glb(path, doc, alter_accessor(doc, binary, bind, translated))
    assert "E_GLB_REST_RECONSTRUCTION" in {e["code"] for e in inspect_glb(path)["errors"]}
    doc, binary = make_glb(path, skinned=True)
    joint_index = doc["meshes"][0]["primitives"][0]["attributes"]["JOINTS_0"]
    view = doc["bufferViews"][doc["accessors"][joint_index]["bufferView"]]
    mutable = bytearray(binary)
    mutable[view["byteOffset"] + 3*4] = 1
    _write_glb(path, doc, bytes(mutable))
    assert "E_GLB_SEAM_WEIGHTS" in {e["code"] for e in inspect_glb(path)["errors"]}


def test_detects_nonfinite_uv_external_image_and_sampling(tmp_path):
    path = tmp_path / "bad.glb"
    doc, binary = make_glb(path)
    uv = doc["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]
    binary = alter_accessor(doc, binary, uv, [float("nan"), .5])
    doc["images"][0]["uri"] = "external.png"
    doc["samplers"][0]["minFilter"] = 9987
    _write_glb(path, doc, binary)
    result = inspect_glb(path)
    assert {e["code"] for e in result["errors"]} >= {"E_GLB_UV_INVALID", "E_GLB_IMAGE_NOT_EMBEDDED_PNG", "E_GLB_PIXEL_SAMPLER"}


def test_truncated_and_out_of_range_binary_returns_diagnostic(tmp_path):
    path = tmp_path / "truncated.glb"
    doc, binary = make_glb(path)
    path.write_bytes(path.read_bytes()[:-3])
    assert inspect_glb(path)["errors"][0]["code"] == "E_GLB_INVALID"
    doc["accessors"][0]["count"] = 100000
    _write_glb(path, doc, binary)
    assert inspect_glb(path)["errors"][0]["code"] == "E_GLB_INVALID"


def test_interleaved_and_sparse_normalized_accessors():
    binary = bytes([255, 0, 0, 0, 0, 128, 0, 0, 1, 255, 255])
    document = {"buffers": [{"byteLength": len(binary)}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 8, "byteStride": 4},
                                {"buffer": 0, "byteOffset": 8, "byteLength": 1},
                                {"buffer": 0, "byteOffset": 9, "byteLength": 2}],
                "accessors": [{"bufferView": 0, "componentType": 5121, "count": 2, "type": "VEC2", "normalized": True},
                              {"componentType": 5121, "count": 2, "type": "VEC2", "normalized": True,
                               "sparse": {"count": 1, "indices": {"bufferView": 1, "componentType": 5121}, "values": {"bufferView": 2}}}]}
    reader = _Binary(document, binary)
    assert reader.accessor(0) == [(1, 0), (0, 128/255)]
    assert reader.accessor(1) == [(0, 0), (1, 1)]


@pytest.mark.blender
@pytest.mark.parametrize("position,yaw_degrees,uniform_scale",[
    ((0,0,0),0,1),
    ((1.25,-2,.5),90,2),
],ids=["canonical","translated_yaw90_scale2"])
def test_real_blender_character_export(tmp_path,position,yaw_degrees,uniform_scale):
    bpy = pytest.importorskip("bpy")
    from b3dtile.atlas import build_atlases
    from b3dtile.geometry import build_kit
    from b3dtile.glb import (export_glb,_world_matrices,_transform,_skin_points,
                            _matrix,_multiply,_interpolate)
    from b3dtile.materials import AtlasMaterials
    from b3dtile.rig import bind_humanoid, create_smoke_animation

    bpy.ops.wm.read_factory_settings(use_empty=True)
    catalog = json.loads((ROOT / "catalog/starter_v1.catalog.json").read_text(encoding="utf-8"))
    paths = build_atlases(catalog, {"seed": 42, "ops": []}, tmp_path)
    instance = build_kit("humanoid_round_v1", "zombie", catalog)
    instance.root.location=position
    instance.root.rotation_euler.z=math.radians(yaw_degrees)
    instance.root.scale=(uniform_scale,)*3
    bpy.context.view_layer.update()
    AtlasMaterials(catalog, paths).apply_defaults(instance)
    bind_humanoid(instance, "humanoid_20_v1", catalog)
    create_smoke_animation(instance)
    # Capture independent native Blender world-space ground truth before
    # export. Keep vertex IDs so the same rest correspondence is used for
    # animated comparisons despite glTF duplicating vertices at UV seams.
    source_frames={}
    for frame in [1,13,25,37,49]:
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        evaluated=instance.mesh.evaluated_get(bpy.context.evaluated_depsgraph_get())
        source_mesh=evaluated.to_mesh()
        try:
            world_points=[evaluated.matrix_world@vertex.co for vertex in source_mesh.vertices]
            source_frames[frame]=[(p.x,p.z,-p.y) for p in world_points]
        finally:
            evaluated.to_mesh_clear()
    bpy.context.scene.frame_set(1)
    # An unrelated preview object must never leak into the asset export.
    bpy.ops.mesh.primitive_cube_add(size=10, location=(20, 20, 20))
    bpy.context.object.name = "unrelated_preview_mesh"
    report = export_glb({"zombie": instance}, tmp_path / "real.glb", True)
    doc, binary = _read_glb(tmp_path / "real.glb")
    reader=_Binary(doc,binary)
    mesh_node=next(n for n in doc["nodes"] if "mesh" in n and "skin" in n)
    skin=doc["skins"][mesh_node["skin"]]
    attributes=doc["meshes"][mesh_node["mesh"]]["primitives"][0]["attributes"]
    positions=reader.accessor(attributes["POSITION"])
    weights=reader.accessor(attributes["WEIGHTS_0"])
    joints=reader.accessor(attributes["JOINTS_0"])
    inverse=[_matrix(row) for row in reader.accessor(skin["inverseBindMatrices"])]
    correspondence=[min(range(len(source_frames[1])),key=lambda i:math.dist(p,source_frames[1][i])) for p in positions]
    assert max(math.dist(p,source_frames[1][i]) for p,i in zip(positions,correspondence))<1e-5
    animation=doc["animations"][0]
    native_reference_max_error=0.
    for frame,reference in source_frames.items():
        overrides={}
        for channel in animation["channels"]:
            target=channel["target"];sampler=animation["samplers"][channel["sampler"]]
            value=_interpolate(reader.accessor(sampler["input"]),reader.accessor(sampler["output"]),
                frame/24,sampler.get("interpolation","LINEAR"),target["path"]=="rotation")
            overrides.setdefault(target["node"],{})[target["path"]]=value
        worlds=_world_matrices(doc["nodes"],overrides)
        cpu_points=_skin_points(positions,joints,weights,
            [_multiply(worlds[joint],bind) for joint,bind in zip(skin["joints"],inverse)])
        error=max(math.dist(p,reference[i]) for p,i in zip(cpu_points,correspondence))
        native_reference_max_error=max(native_reference_max_error,error)
        assert error<1e-5,{"frame":frame,"max_error_m":error}
    assert report["ok"],{"report":report,"native_reference_max_error_m":native_reference_max_error}
    assert report["metrics"]["meshes"] == 1
    assert report["metrics"]["skins"] == 1
    assert report["metrics"]["animations"] >= 1
    assert report["metrics"]["max_rest_error_m"] < 1e-5
    assert report["metrics"]["animation_max_displacement_m"] > .1
    assert report["metrics"]["sampled_skin_poses"] >= 3
    assert "unrelated_preview_mesh" not in {node.get("name") for node in doc["nodes"]}
    # Verify the actual binary's hierarchy retains the requested placement,
    # in addition to inspecting reconstructed rest and sampled animated skin.
    # Blender XYZ -> glTF X,Z,-Y. Positive Blender yaw rotates +X toward -Z.
    root_index=next(i for i,node in enumerate(doc["nodes"]) if node.get("name")=="zombie")
    world=_world_matrices(doc["nodes"])[root_index]
    x,y,z=position;angle=math.radians(yaw_degrees)
    assert _transform(world,(0,0,0))==pytest.approx((x,z,-y),abs=1e-6)
    assert _transform(world,(1,0,0))==pytest.approx(
        (x+uniform_scale*math.cos(angle),z,-y-uniform_scale*math.sin(angle)),abs=1e-6)
    assert _transform(world,(0,1,0))==pytest.approx((x,z+uniform_scale,-y),abs=1e-6)

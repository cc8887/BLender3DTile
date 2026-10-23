"""Actual COLOR_0/UV animation bytes and certified rig/weight combinations."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from b3dtile.contracts import load_catalog, validate_request, ContractError
from b3dtile.glb import _read_glb, _write_glb, _Binary, inspect_glb, export_glb
from b3dtile.rig_presets import RIG_IDS, WEIGHT_IDS, skeleton
from b3dtile.uv_animation import frame_index

ROOT = Path(__file__).resolve().parents[1]


def request(extra):
    data = json.loads((ROOT / "examples/house.request.json").read_text())
    data["params"]["ops"][-1:-1] = extra
    return data


def test_semantics_reject_wrong_slot_duplicate_animation_and_later_uv_changes():
    catalog = load_catalog()
    good = {"op": "set_uv_animation", "mesh": "window_r", "region": "glass", "preset": "signal_cycle_v1", "fps": 4}
    validate_request(request([good]), catalog)
    bad = {**good, "mesh": "roof", "region": "roof_left"}
    with pytest.raises(ContractError, match="Flipbook incompatible"):
        validate_request(request([bad]), catalog)
    with pytest.raises(ContractError, match="one track per region"):
        validate_request(request([good, good]), catalog)
    edit = {"op": "set_region_uv", "mesh": "window_r", "region": "glass", "tile_id": "env_glass_blue", "orientation": 0}
    with pytest.raises(ContractError, match="one track per region"):
        validate_request(request([good, edit]), catalog)


def test_flipbook_boundary_and_loop_sampling():
    track = {"fps": 4, "frames": ["a", "b", "c", "b"]}
    assert [frame_index(track, t) for t in (0, .249, .25, .5, .75, 1, 1.25)] == [0, 0, 1, 2, 3, 0, 1]
    for t in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError): frame_index(track, t)


@pytest.fixture
def backend(tmp_path):
    bpy = pytest.importorskip("bpy")
    from b3dtile.atlas import build_atlases
    from b3dtile.materials import AtlasMaterials
    bpy.ops.wm.read_factory_settings(use_empty=True)
    catalog = load_catalog()
    paths = build_atlases(catalog, {"seed": 17, "ops": []}, tmp_path / "textures")
    return bpy, catalog, AtlasMaterials(catalog, paths)


@pytest.mark.parametrize("rig", RIG_IDS)
@pytest.mark.parametrize("weights", WEIGHT_IDS)
def test_every_rig_weight_pair_deforms_and_survives_glb(backend, tmp_path, rig, weights):
    from b3dtile.geometry import build_kit
    from b3dtile.rig import bind_humanoid, validate_rig, create_smoke_animation
    bpy, catalog, materials = backend
    character = build_kit("humanoid_round_v1", "hero", catalog)
    materials.apply_defaults(character)
    bind_humanoid(character, rig, catalog, weights)
    result = validate_rig(character)
    assert result["ok"], result
    assert result["metrics"]["bones"] == len(skeleton(rig))
    assert result["metrics"]["max_influences"] <= 4
    create_smoke_animation(character)
    path = tmp_path / "character.glb"
    report = export_glb({"hero": character}, path, True)
    assert report["ok"], report
    doc, _ = _read_glb(path)
    names = {doc["nodes"][j]["name"] for j in doc["skins"][0]["joints"]}
    assert names == set(skeleton(rig))
    assert report["metrics"]["animation_max_displacement_m"] > .05


def test_vertex_color_is_region_local_and_exported_in_linear_space(backend, tmp_path):
    from b3dtile.geometry import build_kit
    from b3dtile.vertex_colors import paint_vertex_color, validate_vertex_colors, ATTRIBUTE
    from b3dtile.observation import _fingerprints
    bpy, catalog, materials = backend
    window = build_kit("window_square_v1", "window", catalog)
    materials.apply_defaults(window)
    before = _fingerprints(window)
    paint_vertex_color(window, "frame", "height_gradient", "pastel16", 3, 5, catalog)
    after = _fingerprints(window)
    assert before["geometry_sha256"] == after["geometry_sha256"]
    assert before["appearance_sha256"] != after["appearance_sha256"]
    mesh = window.mesh.data
    attr = mesh.color_attributes[ATTRIBUTE]
    for pi in window.regions["glass"]:
        assert all(tuple(attr.data[li].color) == (1., 1., 1., 1.) for li in mesh.polygons[pi].loop_indices)
    assert validate_vertex_colors(window)["ok"]
    assert materials.diagnostics(window) == []
    expected = {tuple(round(v, 5) for v in datum.color) for datum in attr.data}
    path = tmp_path / "colors.glb"
    assert export_glb({"window": window}, path, False)["ok"]
    doc, blob = _read_glb(path)
    reader = _Binary(doc, blob)
    actual = {tuple(round(v, 5) for v in c) for m in doc["meshes"] for p in m["primitives"] for c in reader.accessor(p["attributes"]["COLOR_0"])}
    # Blender may quantize colors to normalized 16-bit; compare tolerance.
    assert all(any(max(abs(a-b) for a,b in zip(color, candidate)) < 3e-5 for candidate in actual) for color in expected)


def test_uv_animation_material_isolation_binary_sampling_and_corruption(backend, tmp_path):
    from b3dtile.geometry import build_kit
    from b3dtile.uv_animation import configure, apply_time
    from b3dtile.vertex_colors import paint_vertex_color, ensure_materials
    bpy, catalog, materials = backend
    windows = {n: build_kit("window_square_v1", n, catalog) for n in ("animated", "static")}
    for window in windows.values(): materials.apply_defaults(window)
    track = configure(windows["animated"], "glass", "signal_cycle_v1", 4, catalog, materials)
    paint_vertex_color(windows["animated"], "frame", "solid", "pastel16", 5, 5, catalog)
    for inst in windows.values(): ensure_materials(inst)
    assert all(materials.diagnostics(inst) == [] for inst in windows.values())
    mesh = windows["animated"].mesh.data
    initial = [tuple(v.uv) for v in mesh.uv_layers.active.data]
    stable = [tuple(v.uv) for v in windows["static"].mesh.data.uv_layers.active.data]
    apply_time(windows, .25)
    assert [tuple(v.uv) for v in mesh.uv_layers.active.data] != initial
    assert [tuple(v.uv) for v in windows["static"].mesh.data.uv_layers.active.data] == stable
    apply_time(windows, 1.)
    assert [tuple(v.uv) for v in mesh.uv_layers.active.data] == initial
    path = tmp_path / "animated.glb"
    result = export_glb(windows, path, False)
    assert result["ok"], result
    assert result["metrics"]["uv_animation_channels"] == 1
    doc, blob = _read_glb(path)
    assert "KHR_animation_pointer" in doc["extensionsUsed"]
    material_indices = [i for i,m in enumerate(doc["materials"]) if m.get("extras", {}).get("b3d_uv_track")]
    assert len(material_indices) == 1
    timeline = json.loads((tmp_path / "uv_animation.json").read_text())
    assert timeline["tracks"][0]["frames"] == track["frames"]
    original = deepcopy(doc)
    doc["animations"][-1]["channels"][0]["target"]["extensions"]["KHR_animation_pointer"]["pointer"] = "/nodes/0/translation"
    _write_glb(path, doc, blob)
    assert not inspect_glb(path)["ok"]
    doc = original
    doc["animations"][-1]["samplers"][0]["interpolation"] = "LINEAR"
    _write_glb(path, doc, blob)
    assert not inspect_glb(path)["ok"]


def test_short_flipbook_repeats_until_common_clip_end(backend, tmp_path):
    from b3dtile.geometry import build_kit
    from b3dtile.uv_animation import configure
    from b3dtile.glb import _interpolate
    bpy, catalog, materials = backend
    windows = {name: build_kit("window_square_v1", name, catalog) for name in ("fast", "slow")}
    for instance in windows.values(): materials.apply_defaults(instance)
    fast = configure(windows["fast"], "glass", "signal_cycle_v1", 8, catalog, materials)
    configure(windows["slow"], "glass", "signal_blink_v1", 1, catalog, materials)
    path = tmp_path / "two_speeds.glb"
    result = export_glb(windows, path, False)
    assert result["ok"], result
    doc, blob = _read_glb(path)
    reader = _Binary(doc, blob)
    clip = doc["animations"][-1]
    channel = clip["channels"][0]
    sampler = clip["samplers"][channel["sampler"]]
    times, offsets = reader.accessor(sampler["input"]), reader.accessor(sampler["output"])
    assert times[-1] == (2.,)
    assert len(times) == 17  # Four fast periods during the two-second clip.
    for t in (0., .125, .5, .625, 1.125, 1.875, 2.):
        expected = fast["offsets_gltf"][frame_index(fast, t)]
        assert _interpolate(times, offsets, t, "STEP") == pytest.approx(expected)

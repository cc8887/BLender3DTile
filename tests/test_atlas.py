"""Regression tests for content addressing, padding and restricted pixel edits."""
import json
from pathlib import Path

import pytest
from PIL import Image, ImageColor

from b3dtile.atlas import build_atlases, build_debug_atlases


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog():
    return json.loads((ROOT / "catalog/starter_v1.catalog.json").read_text(encoding="utf-8"))


def recipe(*ops, seed=42):
    return {"seed": seed, "ops": list(ops)}


def test_reproducible_independent_of_op_order_and_place(catalog, tmp_path):
    a = {"op": "paint_tile", "tile_id": "env_wall_cream", "pattern": "plaster", "palette": "pastel16"}
    b = {"op": "paint_tile", "tile_id": "env_wood_oak", "pattern": "planks", "palette": "pastel16"}
    p1 = build_atlases(catalog, recipe(a, b), tmp_path / "a")
    p2 = build_atlases(catalog, recipe(b, {"op": "place"}, a), tmp_path / "b")
    assert all(p1[key].read_bytes() == p2[key].read_bytes() for key in p1)
    p3 = build_atlases(catalog, recipe(a, b, seed=43), tmp_path / "c")
    assert p1["environment_v1"].read_bytes() != p3["environment_v1"].read_bytes()


def test_marks_inner_coordinates_and_full_boundary_extrusion(catalog, tmp_path):
    op = {"op": "paint_tile", "tile_id": "env_wall_cream", "pattern": "solid", "palette": "pastel16", "marks": [
        {"x": 0, "y": 0, "color_index": 0}, {"x": 27, "y": 27, "color_index": 1}]}
    paths = build_atlases(catalog, recipe(op), tmp_path)
    im = Image.open(paths["environment_v1"])
    palette = [ImageColor.getrgb(c) for c in catalog["palettes"]["pastel16"]]
    assert im.getpixel((2, 2)) == palette[0]
    assert im.getpixel((0, 0)) == palette[0]
    assert im.getpixel((29, 29)) == palette[1]
    assert im.getpixel((31, 31)) == palette[1]
    for tile in catalog["tiles"]:
        if tile["atlas_id"] != "environment_v1":
            continue
        x, y, w, h = tile["rect_px"]
        pad = tile["padding_px"]
        for py in range(h):
            for px in range(w):
                inner_x = min(w - pad - 1, max(pad, px))
                inner_y = min(h - pad - 1, max(pad, py))
                assert im.getpixel((x + px, y + py)) == im.getpixel((x + inner_x, y + inner_y))


@pytest.mark.parametrize("mark", [
    {"x": 28, "y": 0, "color_index": 0},
    {"x": -1, "y": 0, "color_index": 0},
    {"x": 0, "y": 0, "color_index": 16},
    {"x": .5, "y": 0, "color_index": 0},
])
def test_mark_bounds_are_content_bounds(catalog, tmp_path, mark):
    op = {"op": "paint_tile", "tile_id": "env_wall_cream", "pattern": "solid", "palette": "pastel16", "marks": [mark]}
    with pytest.raises(ValueError, match="E_PIXEL_BOUNDS"):
        build_atlases(catalog, recipe(op), tmp_path)


def test_patterns_use_only_the_selected_palette(catalog, tmp_path):
    for tile in catalog["tiles"]:
        for pattern in tile["patterns"]:
            op = {"op": "paint_tile", "tile_id": tile["id"], "pattern": pattern, "palette": tile["default_palette"]}
            paths = build_atlases(catalog, recipe(op), tmp_path)
            im = Image.open(paths[tile["atlas_id"]])
            x, y, w, h = tile["rect_px"]
            pixels = {im.getpixel((x + px, y + py)) for px in range(w) for py in range(h)}
            allowed = {ImageColor.getrgb(c) for c in catalog["palettes"][tile["default_palette"]]}
            assert pixels <= allowed
            if pattern != "solid":
                assert len(pixels) > 1, (tile["id"], pattern)


def test_reject_duplicate_paint_and_pattern_mismatch(catalog, tmp_path):
    op = {"op": "paint_tile", "tile_id": "env_wall_cream", "pattern": "plaster", "palette": "pastel16"}
    with pytest.raises(ValueError, match="E_DUPLICATE_PAINT"):
        build_atlases(catalog, recipe(op, op), tmp_path)
    with pytest.raises(ValueError, match="E_TILE_PATTERN"):
        build_atlases(catalog, recipe(dict(op, pattern="denim")), tmp_path)


def test_debug_tiles_have_direction_and_corner_markers(catalog, tmp_path):
    paths = build_debug_atlases(catalog, tmp_path)
    for tile in catalog["tiles"]:
        im = Image.open(paths[tile["atlas_id"]])
        x, y, w, h = tile["rect_px"]
        pad = tile["padding_px"]
        assert im.size == (256, 256)
        assert im.getpixel((x + pad, y + pad)) == (255, 255, 255)
        assert im.getpixel((x + w - pad - 1, y + h - pad - 1)) == (0, 0, 0)


def test_materials_restrict_uv_and_restore_debug_images(catalog, tmp_path):
    bpy = pytest.importorskip("bpy")
    from b3dtile.materials import AtlasMaterials
    from b3dtile.model import KitInstance

    bpy.ops.wm.read_factory_settings(use_empty=True)
    paths = build_atlases(catalog, recipe(), tmp_path)
    materials = AtlasMaterials(catalog, paths)
    mesh = bpy.data.meshes.new("quad")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)], [], [(0, 1, 2, 3)])
    obj = bpy.data.objects.new("test_wall", mesh)
    bpy.context.collection.objects.link(obj)
    instance = KitInstance("wall", "house_shell_2m_v1", obj, obj,
                           {"wall_front": [0]}, {0: (0, 0), 1: (1, 0), 2: (1, 1), 3: (0, 1)})
    materials.apply_defaults(instance)
    assert materials.diagnostics(instance) == []
    u, v = mesh.uv_layers.active.data[0].uv
    assert abs(u - 2.5 / 256) < 1e-7
    assert abs(v - (1 - 2.5 / 256)) < 1e-7
    assert len(mesh.materials) == 1
    with pytest.raises(ValueError, match="E_REGION_TILE_MISMATCH"):
        materials.set_region_uv(instance, "wall_front", "char_skin_mint", 0)
    with pytest.raises(ValueError, match="E_REGION_SLOT_MISMATCH"):
        materials.assign_slot(instance, "wall_front", "shirt")
    materials.set_region_uv(instance, "wall_front", "env_wall_cream", 90)
    assert tuple(mesh.uv_layers.active.data[0].uv) == pytest.approx((29.5 / 256, 1 - 2.5 / 256))
    debug_paths = build_debug_atlases(catalog, tmp_path)
    original = materials.texture_nodes["environment_v1"].image
    with pytest.raises(RuntimeError):
        with materials.debug_images(debug_paths):
            assert materials.texture_nodes["environment_v1"].image != original
            raise RuntimeError("preview failed")
    assert materials.texture_nodes["environment_v1"].image == original
    assert original.packed_file is not None
    mesh.uv_layers.active.data[0].uv = (0, 0)
    assert materials.diagnostics(instance)[0]["code"] == "E_UV_OUT_OF_REGION"

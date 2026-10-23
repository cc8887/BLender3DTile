"""Deterministic pixel-art atlas construction, independent of Blender.

All drawing takes place in a tile's inner content rectangle. Padding is then
extruded from its boundary pixels. No resizing, anti-aliasing, or free UVs are
involved; every visible texel belongs to the selected 16-colour palette.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import random

from PIL import Image, ImageColor, ImageDraw


_ROLES = {
    "pastel16": {
        "wall": (4, 3, 5), "roof": (6, 8, 7), "wood": (9, 8, 10),
        "stone": (11, 0, 12), "metal": (0, 1, 2), "glass": (14, 11, 15),
        "skin": (12, 11, 13), "face": (12, 0, 13), "shirt": (2, 1, 3),
        "jeans": (14, 1, 15), "shoes": (0, 1, 2),
    },
    "zombie16": {
        "wall": (14, 13, 15), "roof": (3, 2, 4), "wood": (7, 6, 8),
        "stone": (11, 10, 12), "metal": (0, 1, 2), "glass": (12, 10, 13),
        "skin": (8, 7, 9), "face": (8, 0, 9), "shirt": (3, 2, 4),
        "jeans": (11, 10, 12), "shoes": (0, 1, 2),
    },
}


def _palette(catalog: dict, palette_id: str) -> list[tuple[int, int, int]]:
    colors = catalog["palettes"][palette_id]
    if len(colors) != 16:
        raise ValueError(f"E_PALETTE_SIZE: {palette_id} requires 16 colours")
    return [ImageColor.getrgb(color) for color in colors]


def _tile_seed(seed: int, tile_id: str) -> int:
    # Python's hash() is process-randomized, so it cannot be used here.
    return int.from_bytes(sha256(f"atlas-v1:{seed}:{tile_id}".encode()).digest()[:8], "big")


def _paint_content(catalog: dict, tile: dict, pattern: str, palette_id: str, seed: int) -> Image.Image:
    if pattern not in tile["patterns"]:
        raise ValueError(f"E_TILE_PATTERN: {tile['id']} cannot use {pattern}")
    padding = tile["padding_px"]
    w, h = (tile["rect_px"][2] - padding * 2, tile["rect_px"][3] - padding * 2)
    colors = _palette(catalog, palette_id)
    base_i, dark_i, light_i = _ROLES[palette_id][tile["slot"]]
    base, dark, light = (colors[base_i], colors[dark_i], colors[light_i])
    if "base_color_index" in tile:
        base = colors[tile["base_color_index"]]
    im = Image.new("RGB", (w, h), base)
    draw = ImageDraw.Draw(im)
    rng = random.Random(_tile_seed(seed, tile["id"]))
    if pattern == "solid":
        pass
    elif pattern == "checker":
        for y in range(0, h, 7):
            for x in range(0, w, 7):
                if (x // 7 + y // 7) % 2:
                    draw.rectangle((x, y, x + 6, y + 6), fill=light)
    elif pattern == "plaster":
        # Sparse pixel clusters preserve the light, friendly plaster surface.
        for _ in range(max(1, w * h // 70)):
            x, y = rng.randrange(w), rng.randrange(h)
            draw.line((x, y, min(w - 1, x + rng.randrange(1, 4)), y), fill=rng.choice((dark, light)))
    elif pattern == "roof_shingles":
        for row, y in enumerate(range(0, h, 7)):
            draw.line((0, y + 6, w - 1, y + 6), fill=dark)
            draw.line((0, y, w - 1, y), fill=light)
            for x in range(-7 if row % 2 else 0, w, 14):
                draw.line((x, y + 1, x, y + 5), fill=dark)
                draw.line((x + 2, y + 1, x + 10, y + 1), fill=light)
    elif pattern == "planks":
        for x in range(0, w, 7):
            draw.line((x, 0, x, h - 1), fill=dark)
            draw.line((x + 1, 0, x + 1, h - 1), fill=light)
            y = rng.randrange(3, max(4, h - 3))
            draw.line((x + 4, y, x + 4, min(h - 1, y + 5)), fill=dark)
            draw.point((x + 5, y + 2), fill=light)
    elif pattern == "skin_speckle":
        for _ in range(max(1, w * h // 130)):
            x, y = rng.randrange(w), rng.randrange(h)
            draw.rectangle((x, y, min(w - 1, x + 1), min(h - 1, y + 1)), fill=rng.choice((dark, light)))
    elif pattern in {"face_zombie", "face_cute"}:
        ink = colors[0]
        highlight = colors[15] if palette_id == "zombie16" else colors[5]
        # Face features are defined in normalized coordinates for stable charts.
        ey = round(h * .40)
        ew, eh = max(3, w // 10), max(4, h // 7)
        for cx in (round(w * .30), round(w * .70)):
            draw.rectangle((cx - ew // 2, ey - eh // 2, cx + ew // 2, ey + eh // 2), fill=ink)
            draw.rectangle((cx - ew // 2 + 1, ey - eh // 2 + 1, cx - ew // 2 + 2, ey - eh // 2 + 2), fill=highlight)
            if pattern == "face_zombie":
                draw.line((cx - ew // 2 - 1, ey - eh // 2 - 1, cx + ew // 2 + 1, ey - eh // 2 - 1), fill=colors[7] if palette_id == "zombie16" else dark, width=2)
        my = round(h * .68)
        if pattern == "face_zombie":
            draw.line((round(w * .32), my, round(w * .68), my), fill=ink, width=2)
            for x in (round(w * .40), round(w * .59)):
                draw.rectangle((x, my + 1, x + 3, my + 4), fill=highlight)
            draw.line((round(w * .80), round(h * .20), round(w * .86), round(h * .27)), fill=ink)
            for offset in (0, 3):
                draw.line((round(w * .80) + offset - 1, round(h * .20) + offset + 2, round(w * .80) + offset + 2, round(h * .20) + offset - 1), fill=ink)
        else:
            draw.line((round(w * .40), my - 1, round(w * .45), my + 2, round(w * .55), my + 2, round(w * .60), my - 1), fill=ink, width=2)
            cheek = colors[4] if palette_id == "zombie16" else colors[7]
            for cx in (round(w * .23), round(w * .77)):
                draw.rectangle((cx - 3, ey + 8, cx + 3, ey + 10), fill=cheek)
    elif pattern == "stitches":
        # Stitches repeat inside the shared chart rather than pretending that
        # the full image is a front-facing garment painting.
        for y in range(8, h, 16):
            for x in range(4, w, 24):
                draw.line((x, y, x + 11, y), fill=dark)
                for stitch in (2, 6, 10):
                    draw.line((x + stitch, y - 2, x + stitch, y + 2), fill=light)
    elif pattern == "denim":
        for y in range(h):
            for x in range((y % 4), w, 8):
                draw.point((x, y), fill=light)
        for x in range(10, w, 32):
            draw.line((x, 0, x, h - 1), fill=dark)
            for y in range(1, h, 5):
                draw.line((x + 2, y, x + 2, y + 1), fill=colors[14] if palette_id == "zombie16" else colors[10])
    else:
        raise ValueError(f"E_PATTERN_UNKNOWN: {pattern}")
    return im


def _paste_extruded(atlas: Image.Image, tile: dict, content: Image.Image) -> None:
    x, y, width, height = tile["rect_px"]
    pad = tile["padding_px"]
    w, h = content.size
    if (w + pad * 2, h + pad * 2) != (width, height):
        raise ValueError(f"E_TILE_SIZE: {tile['id']}")
    # Include corners by clamping both coordinates, preventing dark seams.
    pixels = content.load()
    expanded = Image.new("RGB", (width, height))
    dest = expanded.load()
    for py in range(height):
        for px in range(width):
            dest[px, py] = pixels[min(w - 1, max(0, px - pad)), min(h - 1, max(0, py - pad))]
    atlas.paste(expanded, (x, y))


def build_atlases(catalog: dict, recipe: dict, output_dir: Path) -> dict[str, Path]:
    """Initialize defaults and apply each recipe tile override exactly once.

    Accepts either a build request envelope or its params. Pixel mark positions
    are relative to the INNER content rectangle, starting at zero.
    """
    recipe = recipe.get("params", recipe)
    seed = recipe.get("seed", 0)
    atlases = {a["id"]: Image.new("RGB", tuple(a["size_px"]), (0, 0, 0)) for a in catalog["atlases"]}
    tiles = {tile["id"]: tile for tile in catalog["tiles"]}
    overrides = {}
    for op in recipe.get("ops", []):
        if op["op"] != "paint_tile":
            continue
        tile_id = op["tile_id"]
        if tile_id in overrides:
            raise ValueError(f"E_DUPLICATE_PAINT: {tile_id}")
        if tile_id not in tiles:
            raise ValueError(f"E_TILE_UNKNOWN: {tile_id}")
        overrides[tile_id] = op
    for tile in tiles.values():
        op = overrides.get(tile["id"], {})
        palette_id = op.get("palette", tile["default_palette"])
        content = _paint_content(catalog, tile, op.get("pattern", tile["default_pattern"]), palette_id, seed)
        colors = _palette(catalog, palette_id)
        for mark in op.get("marks", []):
            x, y, index = mark["x"], mark["y"], mark["color_index"]
            if any(type(value) is not int for value in (x, y, index)) or not (0 <= x < content.width and 0 <= y < content.height and 0 <= index < 16):
                raise ValueError(f"E_PIXEL_BOUNDS: {tile['id']} mark {mark}")
            content.putpixel((x, y), colors[index])
        _paste_extruded(atlases[tile["atlas_id"]], tile, content)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for atlas_id, im in atlases.items():
        paths[atlas_id] = output_dir / f"{atlas_id}.png"
        im.save(paths[atlas_id], format="PNG", optimize=False)
    return paths


def build_debug_atlases(catalog: dict, output_dir: Path) -> dict[str, Path]:
    """Create tile IDs, checkerboards and upward arrows for UV diagnosis.

    The arrow denotes decreasing local T (image up). The upper-left marker is
    white and lower-right marker black, making mirrored/rotated charts visible.
    """
    atlases = {a["id"]: Image.new("RGB", tuple(a["size_px"]), (18, 20, 28)) for a in catalog["atlases"]}
    colors = [(231, 111, 81), (42, 157, 143), (233, 196, 106), (122, 162, 247), (187, 154, 247), (244, 162, 97), (122, 200, 130), (245, 120, 180), (116, 192, 221), (180, 170, 95), (165, 170, 190)]
    for index, tile in enumerate(catalog["tiles"]):
        pad = tile["padding_px"]
        w, h = tile["rect_px"][2] - pad * 2, tile["rect_px"][3] - pad * 2
        color = colors[index % len(colors)]
        im = Image.new("RGB", (w, h), color)
        draw = ImageDraw.Draw(im)
        for y in range(0, h, 8):
            for x in range(0, w, 8):
                if (x // 8 + y // 8) % 2:
                    draw.rectangle((x, y, x + 7, y + 7), fill=tuple(c * 3 // 4 for c in color))
        cx = w // 2
        draw.line((cx, h * 3 // 4, cx, h // 4), fill="white", width=2)
        draw.line((cx - 4, h // 4 + 4, cx, h // 4, cx + 4, h // 4 + 4), fill="white", width=2)
        draw.rectangle((0, 0, 2, 2), fill="white")
        draw.rectangle((w - 3, h - 3, w - 1, h - 1), fill="black")
        draw.text((2, max(1, h - 12)), str(index + 1), fill="white", stroke_width=1, stroke_fill="black")
        _paste_extruded(atlases[tile["atlas_id"]], tile, im)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for atlas_id, im in atlases.items():
        paths[atlas_id] = output_dir / f"{atlas_id}_uv_debug.png"
        im.save(paths[atlas_id], format="PNG", optimize=False)
    return paths

"""Revision-bound visual and structured observations of a completed asset.

The public entry point runs inside an isolated Blender worker.  Importing this
module does not import bpy, so ordinary Python clients can read observations.
All image paths are relative to the build directory; hashes let a client reject
stale or partially copied previews before reasoning about the current asset.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable


CAMERAS = {
    "front": (0.0, -1.0, 0.0),
    "side": (1.0, 0.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "three_quarter": (1.0, -1.4, 0.9),
}
IMAGE_SIZE = 384


def bounds(points: Iterable[Iterable[float]]) -> dict:
    """Return a finite world-space AABB, with useful redundant AI-facing fields."""
    values = [tuple(float(v) for v in point) for point in points]
    if not values or any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in values):
        raise ValueError("An observation requires finite three-dimensional geometry")
    lo = [min(p[axis] for p in values) for axis in range(3)]
    hi = [max(p[axis] for p in values) for axis in range(3)]
    return {
        "min": lo,
        "max": hi,
        "center": [(a + b) / 2 for a, b in zip(lo, hi)],
        "dimensions": [b - a for a, b in zip(lo, hi)],
    }


def id_color(object_id: str) -> tuple[int, int, int]:
    """Stable high-contrast RGB label color, independent of object creation order."""
    digest = hashlib.sha256(object_id.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:4], "big") / 2**32
    saturation = 0.6 + digest[4] / 255 * 0.3
    value = 0.75 + digest[5] / 255 * 0.25
    return tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, saturation, value))


def _unique_colors(object_ids: Iterable[str]) -> dict[str, tuple[int, int, int]]:
    result = {}
    occupied = set()
    for object_id in sorted(object_ids):
        attempt = 0
        color = id_color(object_id)
        while color in occupied:
            attempt += 1
            color = id_color(f"{object_id}:{attempt}")
        occupied.add(color)
        result[object_id] = color
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprints(instance, texture_hashes: dict | None = None) -> dict:
    """Hash actual authored geometry, UVs and pixels, independent of recipe intent.

    World transforms are intentionally separate in the object observation.  A
    paint change using the same tile/slot must still change appearance_sha256.
    The optional cache avoids re-reading shared atlas buffers for every kit.
    """
    from array import array
    import sys

    texture_hashes = {} if texture_hashes is None else texture_hashes
    mesh = instance.mesh.data

    def digest_json(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode("utf-8")).hexdigest()

    def image_digest(image):
        key = image.as_pointer()
        if key not in texture_hashes:
            pixels = array("f", [0.0]) * len(image.pixels)
            image.pixels.foreach_get(pixels)
            if sys.byteorder != "little":
                pixels.byteswap()
            pixel_hash = hashlib.sha256(pixels.tobytes()).hexdigest()
            texture_hashes[key] = {"sha256": pixel_hash, "dimensions": list(image.size),
                                   "color_space": image.colorspace_settings.name}
        return texture_hashes[key]

    geometry = {
        "vertices": [list(vertex.co) for vertex in mesh.vertices],
        "polygons": [list(polygon.vertices) for polygon in mesh.polygons],
    }
    materials = []
    for material in mesh.materials:
        textures = []
        if material and material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    textures.append({"atlas_id": node.image.get("atlas_id"),
                                     "image": image_digest(node.image),
                                     "interpolation": node.interpolation})
        materials.append(textures)
    appearance = {
        "uvs": [list(loop.uv) for loop in mesh.uv_layers.active.data] if mesh.uv_layers.active else [],
        "polygon_material_indices": [polygon.material_index for polygon in mesh.polygons],
        "materials": materials,
        "region_slots": instance.region_slots,
        "region_tiles": instance.region_tiles,
        "vertex_colors": {a.name: [list(d.color) for d in a.data] for a in mesh.color_attributes},
        "uv_animations": instance.meta.get("uv_animations", {}),
    }
    return {"geometry_sha256": digest_json(geometry), "appearance_sha256": digest_json(appearance)}


def _image_entry(path: Path, output_dir: Path, revision: str, **metadata) -> dict:
    from PIL import Image

    with Image.open(path) as image:
        dimensions = list(image.size)
    return {
        "path": path.relative_to(output_dir).as_posix(),
        "revision": revision,
        "sha256": _sha256(path),
        "dimensions": dimensions,
        **metadata,
    }


def _evaluated_points(instance, depsgraph) -> list:
    obj = instance.mesh.evaluated_get(depsgraph)
    mesh = obj.to_mesh()
    try:
        return [obj.matrix_world @ vertex.co for vertex in mesh.vertices]
    finally:
        obj.to_mesh_clear()


def _linear(channel: int) -> float:
    channel = channel / 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _make_material(bpy, name: str, color, emission=False):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    shader = nodes.new("ShaderNodeEmission" if emission else "ShaderNodeBsdfPrincipled")
    if emission:
        shader.inputs["Color"].default_value = (*color, 1)
        shader.inputs["Strength"].default_value = 1
    else:
        shader.inputs["Base Color"].default_value = (*color, 1)
        shader.inputs["Roughness"].default_value = 0.82
    material.node_tree.links.new(shader.outputs[0], output.inputs["Surface"])
    return material


def _contact_sheet(images: list[dict], output_dir: Path, revision: str, filename: str,
                   columns: int = 3) -> dict:
    from PIL import Image, ImageDraw, ImageFont

    width = 1024
    cell_width = width // columns
    label_height = 30
    header_height = 62
    rows = math.ceil(len(images) / columns)
    canvas = Image.new("RGB", (width, header_height + rows * (cell_width + label_height)), "#17202d")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=16)
    small = ImageFont.load_default(size=13)
    draw.text((16, 12), "B3D TILE | CURRENT ASSET OBSERVATION", font=font, fill="#f2f4f6")
    draw.text((16, 36), f"revision {revision[:32]} | +Z up / front -Y", font=small, fill="#b4c2d5")
    placements = []
    for index, entry in enumerate(images):
        x = (index % columns) * cell_width
        y = header_height + (index // columns) * (cell_width + label_height)
        with Image.open(output_dir / entry["path"]) as source:
            thumb = source.convert("RGB")
            thumb.thumbnail((cell_width - 4, cell_width - 4), Image.Resampling.NEAREST)
            left = x + (cell_width - thumb.width) // 2
            top = y + (cell_width - thumb.height) // 2
            canvas.paste(thumb, (left, top))
        title = f"{entry.get('mode', 'atlas')} | {entry.get('camera', 'UV')} | {entry.get('pose', 'rest')}"
        if "time_seconds" in entry:
            title = f"UV t={entry['time_seconds']:.3f}s | {entry.get('camera')}"
        draw.text((x + 8, y + cell_width + 5), title, font=small, fill="#ecf0f6")
        placements.append({"image": entry["path"], "rect_px": [x, y, cell_width, cell_width + label_height],
                           "rect_format": "x_y_width_height"})
    path = output_dir / "observations" / filename
    canvas.save(path)
    return _image_entry(path, output_dir, revision, mode="contact_sheet", camera=None,
                        pose="multiple", panels=placements)


def _atlas_previews(instances, catalog, output_dir, revision) -> list[dict]:
    """Show actual image pixels with real UV edges; no screenshots of editor state."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    atlas_images = {}
    for instance in instances.values():
        for material in instance.mesh.data.materials:
            if material and material.use_nodes:
                for node in material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        atlas_id = material.get("atlas_id") or node.image.get("atlas_id")
                        atlas_id = atlas_id or node.image.name.split(".")[0]
                        atlas_images.setdefault(atlas_id, node.image)
    result = []
    for atlas_id, bpy_image in sorted(atlas_images.items()):
        width, height = bpy_image.size
        values = np.empty(width * height * 4, dtype=np.float32)
        bpy_image.pixels.foreach_get(values)
        values = values.reshape(height, width, 4)[::-1].copy()
        # Byte PNG buffers with sRGB metadata expose normalized encoded bytes.
        # Float/linear images require an explicit display conversion instead.
        if bpy_image.colorspace_settings.name.lower().startswith("linear"):
            rgb = values[:, :, :3]
            values[:, :, :3] = np.where(
                rgb <= 0.0031308, rgb * 12.92,
                1.055 * np.maximum(rgb, 0) ** (1 / 2.4) - 0.055,
            )
        pixels = np.round(np.clip(values, 0, 1) * 255).astype(np.uint8)
        image = Image.fromarray(pixels, "RGBA").convert("RGB").resize((512, 512), Image.Resampling.NEAREST)
        draw = ImageDraw.Draw(image)
        for instance in instances.values():
            mesh = instance.mesh.data
            if not mesh.uv_layers.active:
                continue
            uvs = mesh.uv_layers.active.data
            for polygon in mesh.polygons:
                if polygon.material_index >= len(mesh.materials):
                    continue
                material = mesh.materials[polygon.material_index]
                if not material or not material.use_nodes:
                    continue
                used_images = [node.image for node in material.node_tree.nodes if node.type == "TEX_IMAGE"]
                if bpy_image not in used_images:
                    continue
                points = [(float(uvs[i].uv.x) * 512, (1 - float(uvs[i].uv.y)) * 512) for i in polygon.loop_indices]
                if points:
                    draw.line(points + [points[0]], fill="#3dfaf0", width=1)
        for tile in catalog.get("tiles", []):
            if tile["atlas_id"] != atlas_id:
                continue
            x, y, w, h = tile["rect_px"]
            box = (x / width * 512, y / height * 512, (x + w) / width * 512 - 1, (y + h) / height * 512 - 1)
            draw.rectangle(box, outline="#ffdf68", width=2)
        # atlas_id comes from trusted material/catalog metadata, never from a path.
        safe_id = "".join(c if c.isalnum() or c in "_-" else "_" for c in atlas_id)
        path = output_dir / "observations" / f"atlas_{safe_id}_uv.png"
        image.save(path)
        result.append(_image_entry(path, output_dir, revision, mode="atlas_uv", camera=None,
                                   pose="rest", atlas_id=atlas_id,
                                   legend={"cyan": "actual mesh UV edges", "yellow": "catalog tile boundaries"}))
    return result


def capture_observation(instances: dict, recipe: dict, catalog: dict, output_dir: Path,
                        revision: str, diagnostics: dict) -> dict:
    """Render a consistent observation packet and restore asset scene state.

    A failed render propagates, allowing the worker to avoid publishing a partial
    revision.  Auxiliary objects/materials never survive this call.  Bounding
    boxes/projections are evaluated from the same posed meshes as each image.
    """
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector

    if not instances:
        raise ValueError("Cannot observe an empty asset")
    output_dir = Path(output_dir)
    (output_dir / "observations").mkdir(parents=True, exist_ok=True)
    params = recipe.get("params", recipe)
    scene = bpy.context.scene
    original_camera = scene.camera
    original_world = scene.world
    original_frame = scene.frame_current
    original_render = {key: getattr(scene.render, key) for key in
                       ("engine", "resolution_x", "resolution_y", "resolution_percentage", "filepath", "film_transparent")}
    original_view = {key: getattr(scene.view_settings, key) for key in ("view_transform", "look", "exposure", "gamma")}
    original_color_mode = scene.render.image_settings.color_mode
    original_format = scene.render.image_settings.file_format
    original_cycles = {key: getattr(scene.cycles, key) for key in
                       ("samples", "device", "use_denoising", "seed")}
    originals = {}
    saved_rigs = []
    auxiliary_objects = []
    auxiliary_materials = []
    loaded_debug_images = []
    material_nodes = []
    world = None
    images = []
    colors = _unique_colors(instances)
    rigs = [instance for instance in instances.values() if instance.rig is not None]

    def pose_all(pose):
        if pose != "rest" and not rigs:
            raise ValueError("E_POSE_REQUIRES_RIG: " + pose)
        if rigs:
            from .rig import set_pose
            for instance in rigs:
                set_pose(instance, pose)
        bpy.context.view_layer.update()

    def all_points():
        graph = bpy.context.evaluated_depsgraph_get()
        return {object_id: _evaluated_points(instance, graph) for object_id, instance in instances.items()}

    try:
        # Snapshot original material assignment and exact pose, not just an enum.
        for object_id, instance in instances.items():
            originals[object_id] = {
                "materials": list(instance.mesh.data.materials),
                "material_indices": [p.material_index for p in instance.mesh.data.polygons],
                "hide_render": instance.mesh.hide_render,
            }
            instance.mesh.hide_render = False
        for instance in rigs:
            rig = instance.rig
            action = rig.animation_data.action if rig.animation_data else None
            saved_rigs.append((rig, action, rig.data.pose_position,
                               [(bone, bone.rotation_mode, bone.matrix_basis.copy()) for bone in rig.pose.bones]))
            if rig.animation_data:
                rig.animation_data.action = None
            rig.data.pose_position = "POSE"
        pose_all("rest")
        rest_points = all_points()
        scene_bounds = bounds(point for values in rest_points.values() for point in values)
        objects = []
        texture_hashes = {}
        for object_id, instance in sorted(instances.items()):
            mesh = instance.mesh.data
            mesh.calc_loop_triangles()
            regions = {}
            for region, polygons in instance.regions.items():
                regions[region] = {
                    "polygon_count": len(polygons),
                    "slot": instance.region_slots.get(region),
                    "tile_id": instance.region_tiles.get(region),
                }
            objects.append({
                "id": object_id,
                "kit_id": instance.kit_id,
                "blender_object": instance.mesh.name,
                "bbox": bounds(rest_points[object_id]),
                "world_matrix": [list(row) for row in instance.mesh.matrix_world],
                "vertices": len(mesh.vertices),
                "triangles": len(mesh.loop_triangles),
                "polygons": len(mesh.polygons),
                "regions": regions,
                "bone_count": len(instance.rig.data.bones) if instance.rig else 0,
                "rig_preset": instance.meta.get("rig_preset"),
                "weight_preset": instance.meta.get("weight_preset"),
                "weight_table_sha256": instance.meta.get("weight_table_sha256"),
                "vertex_colors": instance.meta.get("vertex_colors", {}),
                "uv_animations": [{k: v for k, v in t.items() if k not in ("loop_indices", "base_uv_blender")}
                                  for t in instance.meta.get("uv_animations", {}).values()],
                "bones": [{"name": bone.name, "parent": bone.parent.name if bone.parent else None}
                          for bone in instance.rig.data.bones] if instance.rig else [],
                "id_color_rgb": list(colors[object_id]),
                **_fingerprints(instance, texture_hashes),
            })

        # Fit once to the union of all tested poses, so images remain comparable.
        framing_points = [p.copy() for values in rest_points.values() for p in values]
        pose_names = ["rest"] + (["arms_raise_90", "arms_raise_120", "elbows_bend_90"] if rigs else [])
        for pose in pose_names[1:]:
            pose_all(pose)
            framing_points.extend(p.copy() for values in all_points().values() for p in values)
        pose_all("rest")
        frame_bounds = bounds(framing_points)
        target = Vector(frame_bounds["center"])
        extent = max(max(frame_bounds["dimensions"]), 0.1)

        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 8
        scene.cycles.seed = params.get("seed", 0) % 2147483647
        scene.cycles.use_denoising = True
        scene.render.resolution_x = IMAGE_SIZE
        scene.render.resolution_y = IMAGE_SIZE
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGB"
        scene.render.film_transparent = False
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0
        scene.view_settings.gamma = 1
        world = bpy.data.worlds.new("__observation_world")
        world.use_nodes = True
        world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.18, 0.22, 0.28, 1)
        world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.65
        scene.world = world

        camera_data = bpy.data.cameras.new("__observation_camera")
        camera = bpy.data.objects.new("__observation_camera", camera_data)
        scene.collection.objects.link(camera)
        auxiliary_objects.append(camera)
        camera_data.type = "ORTHO"
        camera_data.clip_start = 0.001
        camera_data.clip_end = extent * 100
        scene.camera = camera
        for name, direction, power, size in [
            ("key", (-3, -4, 5), 350, 4),
            ("fill", (4, -1, 3), 180, 5),
            ("rim", (1, 4, 4), 250, 3),
        ]:
            data = bpy.data.lights.new(f"__observation_{name}", "AREA")
            data.energy = power * extent * extent
            data.shape = "DISK"
            data.size = size * extent
            light = bpy.data.objects.new(data.name, data)
            scene.collection.objects.link(light)
            auxiliary_objects.append(light)
            light.location = target + Vector(direction) * extent
            light.rotation_euler = (target - light.location).to_track_quat("-Z", "Y").to_euler()

        clay = _make_material(bpy, "__observation_clay", (0.45, 0.55, 0.63))
        auxiliary_materials.append(clay)
        wireframe = _make_material(bpy, "__observation_wireframe", (0.45, 0.55, 0.63))
        wire_nodes = wireframe.node_tree.nodes
        wire_node = wire_nodes.new("ShaderNodeWireframe")
        wire_node.use_pixel_size = True
        wire_node.inputs["Size"].default_value = 0.65
        wire_mix = wire_nodes.new("ShaderNodeMixRGB")
        wire_mix.inputs[1].default_value = (0.45, 0.55, 0.63, 1)
        wire_mix.inputs[2].default_value = (0.025, 0.035, 0.05, 1)
        wireframe.node_tree.links.new(wire_node.outputs[0], wire_mix.inputs[0])
        wire_shader = next(node for node in wire_nodes if node.type == "BSDF_PRINCIPLED")
        wireframe.node_tree.links.new(wire_mix.outputs[0], wire_shader.inputs["Base Color"])
        auxiliary_materials.append(wireframe)
        labels = {}
        color_views = {}
        for object_id, color in colors.items():
            labels[object_id] = _make_material(bpy, f"__observation_id_{object_id}",
                                               tuple(_linear(c) for c in color), emission=True)
            auxiliary_materials.append(labels[object_id])
            if instances[object_id].meta.get("vertex_colors"):
                material = _make_material(bpy, "__observation_color_" + object_id, (1, 1, 1), emission=True)
                vertex = material.node_tree.nodes.new("ShaderNodeVertexColor")
                vertex.layer_name = "B3DColor"
                shader = next(n for n in material.node_tree.nodes if n.type == "EMISSION")
                material.node_tree.links.new(vertex.outputs["Color"], shader.inputs["Color"])
            else:
                material = _make_material(bpy, "__observation_color_" + object_id, (1, 1, 1), emission=True)
            color_views[object_id] = material
            auxiliary_materials.append(material)

        def restore_materials():
            for object_id, instance in instances.items():
                mesh = instance.mesh.data
                mesh.materials.clear()
                for material in originals[object_id]["materials"]:
                    mesh.materials.append(material)
                for poly, index in zip(mesh.polygons, originals[object_id]["material_indices"]):
                    poly.material_index = index

        def set_material_mode(mode):
            restore_materials()
            for node, original in material_nodes:
                node.image = original
            if mode in ("clay", "wireframe", "object_id", "vertex_color"):
                for object_id, instance in instances.items():
                    mesh = instance.mesh.data
                    mesh.materials.clear()
                    material = clay if mode == "clay" else wireframe if mode == "wireframe" else color_views[object_id] if mode == "vertex_color" else labels[object_id]
                    mesh.materials.append(material)
                    for polygon in mesh.polygons:
                        polygon.material_index = 0
            elif mode == "uv_debug":
                for node, original in material_nodes:
                    atlas_id = original.get("atlas_id")
                    if atlas_id in debug_images:
                        node.image = debug_images[atlas_id]

        def aim(camera_name):
            direction = Vector(CAMERAS[camera_name]).normalized()
            camera.location = target + direction * extent * 5
            camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
            bpy.context.view_layer.update()
            inverse = camera.matrix_world.inverted()
            projected = [inverse @ point for point in framing_points]
            span_x = max(p.x for p in projected) - min(p.x for p in projected)
            span_y = max(p.y for p in projected) - min(p.y for p in projected)
            offset = Vector(((max(p.x for p in projected) + min(p.x for p in projected)) / 2,
                             (max(p.y for p in projected) + min(p.y for p in projected)) / 2, 0))
            camera.location += camera.matrix_world.to_3x3() @ offset
            camera_data.ortho_scale = max(span_x, span_y, 0.1) * 1.22
            bpy.context.view_layer.update()

        def render(camera_name, pose, mode, time_seconds=None):
            from .uv_animation import apply_time
            apply_time(instances, time_seconds or 0.)
            pose_all(pose)
            set_material_mode(mode)
            aim(camera_name)
            black = mode == "object_id"
            world.node_tree.nodes["Background"].inputs["Color"].default_value = ((0, 0, 0, 1) if black else (0.18, 0.22, 0.28, 1))
            scene.cycles.use_denoising = not black
            suffix = "" if time_seconds is None else f"_uv_time_{time_seconds:.6f}"
            path = output_dir / "observations" / f"{mode}_{camera_name}_{pose}{suffix}.png"
            scene.render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
            projected = {}
            for object_id, points in all_points().items():
                pixels = [world_to_camera_view(scene, camera, p) for p in points]
                xs = [float(p.x) * IMAGE_SIZE for p in pixels]
                ys = [(1 - float(p.y)) * IMAGE_SIZE for p in pixels]
                projected[object_id] = {
                    "rect_px": [min(xs), min(ys), max(xs), max(ys)],
                    "rect_format": "xmin_ymin_xmax_ymax",
                    "center_px": [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2],
                    "bbox_world": bounds(points),
                    "visibility": "projected bounds; may be occluded by other geometry",
                }
            entry = _image_entry(path, output_dir, revision, camera=camera_name, pose=pose,
                                 mode=mode, objects=projected,
                                 camera_world_matrix=[list(row) for row in camera.matrix_world],
                                 orthographic_scale=float(camera_data.ortho_scale))
            if time_seconds is not None:
                from .uv_animation import frame_index, public_tracks
                entry["time_seconds"] = time_seconds
                entry["uv_frames"] = {track["id"]: frame_index(track, time_seconds) for track in public_tracks(instances)}
            images.append(entry)
            return entry

        # Keep full texture pixels and mesh UV evidence before applying diagnostics.
        atlas_images = _atlas_previews(instances, catalog, output_dir, revision)
        from .atlas import build_debug_atlases
        debug_paths = build_debug_atlases(catalog, output_dir / "observations" / "debug_atlases")
        debug_images = {}
        for atlas_id, path in debug_paths.items():
            image = bpy.data.images.load(str(path), check_existing=False)
            image["atlas_id"] = atlas_id
            debug_images[atlas_id] = image
            loaded_debug_images.append(image)
        seen_nodes = set()
        for saved in originals.values():
            for material in saved["materials"]:
                if material and material.use_nodes:
                    for node in material.node_tree.nodes:
                        if node.type == "TEX_IMAGE" and node.image and node.as_pointer() not in seen_nodes:
                            atlas_id = node.image.get("atlas_id") or material.get("atlas_id")
                            if atlas_id:
                                node.image["atlas_id"] = atlas_id
                                material_nodes.append((node, node.image))
                                seen_nodes.add(node.as_pointer())

        for camera_name in CAMERAS:
            render(camera_name, "rest", "beauty")
        render("three_quarter", "rest", "clay")
        render("three_quarter", "rest", "wireframe")
        render("three_quarter", "rest", "object_id")
        render("three_quarter", "rest", "uv_debug")
        if any(i.meta.get("vertex_colors") for i in instances.values()):
            render("three_quarter", "rest", "vertex_color")
        for pose in pose_names[1:]:
            render("front", pose, "beauty")
        requested = params.get("ops", [])
        for op in requested:
            if op.get("op") != "render_preview":
                continue
            mode = "uv_debug" if op["texture_view"] == "uv_debug" else "beauty"
            if not any(i["camera"] == op["camera"] and i["pose"] == op["pose"] and i["mode"] == mode for i in images):
                render(op["camera"], op["pose"], mode)
        images.extend(atlas_images)
        from .uv_animation import public_tracks, apply_time
        tracks = public_tracks(instances)
        animation_images = []
        for seconds in sorted({i / t["fps"] for t in tracks for i in range(len(t["frames"]))}):
            animation_images.append(render("three_quarter", "rest", "beauty", seconds))
        apply_time(instances, 0.)
        if animation_images:
            images.append(_contact_sheet(animation_images, output_dir, revision, "uv_animation_strip.png", columns=4))
        contact = _contact_sheet(images, output_dir, revision, "contact_sheet.png")
        images.append(contact)
        pose_images = [i for i in images if i.get("mode") == "beauty" and i.get("camera") == "front"]
        if rigs:
            images.append(_contact_sheet(pose_images, output_dir, revision, "pose_strip.png", columns=4))
        snapshot = {
            "observation_version": "1.0",
            "revision": revision,
            "asset_id": params.get("asset_id"),
            "asset_kind": params.get("asset_kind"),
            "catalog_version": catalog.get("catalog_version"),
            "coordinate_system": {"units": "meters", "up": "+Z", "front": "-Y", "image_origin": "top_left"},
            "bbox": scene_bounds,
            "framing_bbox": frame_bounds,
            "objects": objects,
            "images": images,
            "contact_sheet": contact["path"],
            "id_legend": {object_id: list(color) for object_id, color in colors.items()},
            "diagnostics": diagnostics,
            "reading_guide": [
                "Verify revision against the latest build pointer before interpreting images.",
                "Start with contact_sheet; beauty shows texture, clay shows geometry, object_id locates stable object IDs.",
                "Object coordinates and default bounds use the rest pose; image objects include their actual posed bounds.",
                "UV debug is an alternate diagnostic texture; atlas_uv overlays actual UV edges on the final texture.",
                "Projected rectangles do not imply visibility; use object_id and beauty to judge occlusion.",
            ],
            "render": {"engine": "CYCLES", "device": "CPU", "samples": 8, "image_size": IMAGE_SIZE,
                       "view_transform": "Standard", "blender_version": bpy.app.version_string},
        }
        (output_dir / "observation.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return snapshot
    finally:
        from .uv_animation import apply_time
        apply_time(instances, 0.)
        for node, original in material_nodes:
            node.image = original
        for object_id, instance in instances.items():
            if object_id not in originals:
                continue
            mesh = instance.mesh.data
            mesh.materials.clear()
            for material in originals[object_id]["materials"]:
                mesh.materials.append(material)
            for polygon, index in zip(mesh.polygons, originals[object_id]["material_indices"]):
                polygon.material_index = index
            instance.mesh.hide_render = originals[object_id]["hide_render"]
        for rig, action, pose_position, poses in saved_rigs:
            rig.data.pose_position = pose_position
            for bone, rotation_mode, matrix in poses:
                bone.rotation_mode = rotation_mode
                bone.matrix_basis = matrix
            if rig.animation_data:
                rig.animation_data.action = action
        scene.frame_set(original_frame)
        scene.camera = original_camera
        scene.world = original_world
        for obj in auxiliary_objects:
            data = obj.data
            kind = obj.type
            bpy.data.objects.remove(obj, do_unlink=True)
            if data.users == 0:
                (bpy.data.cameras if kind == "CAMERA" else bpy.data.lights).remove(data)
        for material in auxiliary_materials:
            bpy.data.materials.remove(material)
        for image in loaded_debug_images:
            bpy.data.images.remove(image)
        if world:
            bpy.data.worlds.remove(world)
        for key, value in original_render.items():
            setattr(scene.render, key, value)
        for key, value in original_view.items():
            setattr(scene.view_settings, key, value)
        for key, value in original_cycles.items():
            setattr(scene.cycles, key, value)
        scene.render.image_settings.file_format = original_format
        scene.render.image_settings.color_mode = original_color_mode
        bpy.context.view_layer.update()

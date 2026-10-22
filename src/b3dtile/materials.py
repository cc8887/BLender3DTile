"""Blender-only adapter for fixed atlas materials and authored UV charts."""
from __future__ import annotations

from contextlib import contextmanager
from math import isfinite
from pathlib import Path

import bpy

from .model import KitInstance


class AtlasMaterials:
    def __init__(self, catalog: dict, atlas_paths: dict[str, Path]):
        self.catalog = catalog
        self.kits = {k["id"]: k for k in catalog["kits"]}
        self.tiles = {t["id"]: t for t in catalog["tiles"]}
        self.slots = {s["id"]: s for s in catalog["slots"]}
        self.atlases = {a["id"]: a for a in catalog["atlases"]}
        self.images = {}
        self.materials = {}
        self.texture_nodes = {}
        for atlas_id, path in atlas_paths.items():
            im = bpy.data.images.load(str(Path(path).resolve()), check_existing=False)
            im.name = atlas_id
            im["atlas_id"] = atlas_id
            im.colorspace_settings.name = "sRGB"
            im.pack()
            mat = bpy.data.materials.new(f"atlas_{atlas_id}")
            mat["atlas_id"] = atlas_id
            mat.use_nodes = True
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            bsdf.inputs["Metallic"].default_value = 0
            bsdf.inputs["Roughness"].default_value = 1
            tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
            tex.name = "AtlasTexture"
            tex.image = im
            tex.interpolation = "Closest"
            tex.extension = "EXTEND"
            mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            self.images[atlas_id] = im
            self.materials[atlas_id] = mat
            self.texture_nodes[atlas_id] = tex

    def _region(self, instance: KitInstance, region: str) -> dict:
        if region not in instance.regions or region not in self.kits[instance.kit_id]["regions"]:
            raise ValueError(f"E_REGION_UNKNOWN: {instance.object_id}.{region}")
        return self.kits[instance.kit_id]["regions"][region]

    def apply_defaults(self, instance: KitInstance) -> None:
        if not instance.mesh.data.uv_layers:
            instance.mesh.data.uv_layers.new(name="UVMap")
        for region in instance.regions:
            self.assign_slot(instance, region, self._region(instance, region)["default_slot"])

    def assign_slot(self, instance: KitInstance, region: str, slot: str) -> None:
        spec = self._region(instance, region)
        if slot not in spec["allowed_slots"]:
            raise ValueError(f"E_REGION_SLOT_MISMATCH: {instance.object_id}.{region} cannot use {slot}")
        instance.region_slots[region] = slot
        self.set_region_uv(instance, region, self.slots[slot]["default_tile"], 0)

    def set_region_uv(self, instance: KitInstance, region: str, tile_id: str, orientation: int = 0) -> None:
        spec = self._region(instance, region)
        slot = instance.region_slots.get(region, spec["default_slot"])
        if tile_id not in self.slots[slot]["allowed_tiles"]:
            raise ValueError(f"E_REGION_TILE_MISMATCH: {instance.object_id}.{region} cannot use {tile_id}")
        if orientation not in spec["allowed_uv_rotations"]:
            raise ValueError(f"E_UV_ROTATION: {instance.object_id}.{region} cannot rotate {orientation}")
        tile = self.tiles[tile_id]
        atlas_id = tile["atlas_id"]
        mat = self.materials[atlas_id]
        mesh = instance.mesh.data
        material_index = next((i for i, item in enumerate(mesh.materials) if item == mat), None)
        if material_index is None:
            material_index = len(mesh.materials)
            mesh.materials.append(mat)
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="UVMap")
        uvs = mesh.uv_layers.active.data
        x, y, w, h = tile["rect_px"]
        pad = tile["padding_px"]
        aw, ah = self.atlases[atlas_id]["size_px"]
        for polygon_index in instance.regions[region]:
            polygon = mesh.polygons[polygon_index]
            polygon.material_index = material_index
            for loop in polygon.loop_indices:
                s, t = instance.uv_template[loop]
                if not (isfinite(s) and isfinite(t) and 0 <= s <= 1 and 0 <= t <= 1):
                    raise ValueError(f"E_UV_TEMPLATE: {instance.object_id}.{region} loop {loop}")
                for _ in range(orientation // 90):
                    s, t = 1 - t, s
                # Map endpoints to centre of first and last INNER pixels;
                # Blender V points upward, while our chart and image T go down.
                uvs[loop].uv = ((x + pad + .5 + s * (w - 2 * pad - 1)) / aw,
                                1 - (y + pad + .5 + t * (h - 2 * pad - 1)) / ah)
        instance.region_slots[region] = slot
        instance.region_tiles[region] = tile_id
        instance.meta.setdefault("region_uv_orientations", {})[region] = orientation
        mesh.update()

    def diagnostics(self, instance: KitInstance) -> list[dict]:
        """Return actionable errors; an empty list means every region passed."""
        errors = []
        mesh = instance.mesh.data
        if not mesh.uv_layers:
            return [{"code": "E_UV_MISSING", "object_id": instance.object_id}]
        assigned = set()
        for region, polygon_indices in instance.regions.items():
            tile_id = instance.region_tiles.get(region)
            if tile_id not in self.tiles:
                errors.append({"code": "E_REGION_TILE_MISSING", "object_id": instance.object_id, "region": region})
                continue
            tile = self.tiles[tile_id]
            atlas_id = tile["atlas_id"]
            x, y, w, h = tile["rect_px"]
            pad = tile["padding_px"]
            aw, ah = self.atlases[atlas_id]["size_px"]
            bounds = ((x + pad + .5) / aw, (x + w - pad - .5) / aw,
                      1 - (y + h - pad - .5) / ah, 1 - (y + pad + .5) / ah)
            for poly_index in polygon_indices:
                if poly_index in assigned:
                    errors.append({"code": "E_REGION_OVERLAP", "object_id": instance.object_id, "region": region, "polygon": poly_index})
                assigned.add(poly_index)
                poly = mesh.polygons[poly_index]
                actual_mat = mesh.materials[poly.material_index] if poly.material_index < len(mesh.materials) else None
                if actual_mat != self.materials[atlas_id]:
                    errors.append({"code": "E_ATLAS_MATERIAL", "object_id": instance.object_id, "region": region, "polygon": poly_index})
                for loop in poly.loop_indices:
                    u, v = mesh.uv_layers.active.data[loop].uv
                    if not (isfinite(u) and isfinite(v) and bounds[0] - 1e-7 <= u <= bounds[1] + 1e-7 and bounds[2] - 1e-7 <= v <= bounds[3] + 1e-7):
                        errors.append({"code": "E_UV_OUT_OF_REGION", "object_id": instance.object_id, "region": region, "loop": loop, "uv": [u, v], "tile_id": tile_id})
        missing = sorted(set(range(len(mesh.polygons))) - assigned)
        if missing:
            errors.append({"code": "E_UNASSIGNED_FACES", "object_id": instance.object_id, "polygons": missing[:16], "count": len(missing)})
        return errors

    @contextmanager
    def debug_images(self, paths: dict[str, Path]):
        """Temporarily swap image nodes; always restore final export textures."""
        previous = {key: node.image for key, node in self.texture_nodes.items()}
        loaded = []
        try:
            for atlas_id, path in paths.items():
                if atlas_id not in self.texture_nodes:
                    continue
                im = bpy.data.images.load(str(Path(path).resolve()), check_existing=False)
                im.colorspace_settings.name = "sRGB"
                loaded.append(im)
                self.texture_nodes[atlas_id].image = im
            yield
        finally:
            for atlas_id, im in previous.items():
                self.texture_nodes[atlas_id].image = im
            for im in loaded:
                bpy.data.images.remove(im)

"""Bounded atlas flipbooks, standard glTF pointer channels and CPU sampling.

UVs remain valid for the first frame in every generic viewer. Animation-aware
viewers need KHR_animation_pointer + KHR_texture_transform. A JSON timeline is
also emitted for engine adapters; no sentinel values are hidden in TEXCOORD_0.
"""
from copy import deepcopy
import math
import re
import struct


def configure(instance, region, preset_id, fps, catalog, materials):
    preset = next(p for p in catalog["uv_animation_presets"] if p["id"] == preset_id)
    tiles = {t["id"]: t for t in catalog["tiles"]}
    frames = [tiles[t] for t in preset["frames"]]
    first = frames[0]
    atlas = next(a for a in catalog["atlases"] if a["id"] == first["atlas_id"])
    materials.set_region_uv(instance, region, first["id"], 0)
    mesh = instance.mesh.data
    index = mesh.polygons[instance.regions[region][0]].material_index
    material = mesh.materials[index].copy()
    track_id = instance.object_id + "." + region
    material.name = "b3d_uv_" + track_id
    material["b3d_uv_track"] = track_id
    mesh.materials.append(material)
    new_index = len(mesh.materials) - 1
    loops = []
    for pi in instance.regions[region]:
        polygon = mesh.polygons[pi]
        polygon.material_index = new_index
        loops.extend(polygon.loop_indices)
    aw, ah = atlas["size_px"]
    x0, y0 = first["rect_px"][:2]
    offsets = [[(t["rect_px"][0] - x0) / aw, (t["rect_px"][1] - y0) / ah] for t in frames]
    track = {"id": track_id, "object_id": instance.object_id, "region": region, "preset": preset_id,
             "fps": fps, "loop": True, "duration_seconds": len(frames) / fps,
             "interpolation": "STEP", "atlas_id": first["atlas_id"],
             "frames": preset["frames"], "offsets_gltf": offsets,
             "base_uv_bounds_gltf": [(x0 + first["padding_px"] + .5) / aw,
                                     (y0 + first["padding_px"] + .5) / ah,
                                     (x0 + first["rect_px"][2] - first["padding_px"] - .5) / aw,
                                     (y0 + first["rect_px"][3] - first["padding_px"] - .5) / ah],
             "loop_indices": loops, "base_uv_blender": [list(mesh.uv_layers.active.data[i].uv) for i in loops]}
    instance.meta.setdefault("uv_animations", {})[region] = track
    return track


def frame_index(track, seconds):
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("E_UV_TIME")
    return int(math.floor(seconds * track["fps"] + 1e-7)) % len(track["frames"])


def apply_time(instances, seconds):
    for instance in instances.values():
        for track in instance.meta.get("uv_animations", {}).values():
            du, dv = track["offsets_gltf"][frame_index(track, seconds)]
            for li, (u, v) in zip(track["loop_indices"], track["base_uv_blender"]):
                instance.mesh.data.uv_layers.active.data[li].uv = (u + du, v - dv)
            instance.mesh.data.update()


def public_tracks(instances):
    return [{k: deepcopy(v) for k, v in track.items() if k not in ("loop_indices", "base_uv_blender")}
            for instance in instances.values() for track in instance.meta.get("uv_animations", {}).values()]


def append_to_glb(document, binary, instances):
    tracks = public_tracks(instances)
    if not tracks:
        return binary, {"version": "1.0", "tracks": []}
    blob = bytearray(binary[:document["buffers"][0]["byteLength"]])
    def accessor(rows, kind):
        blob.extend(b"\0" * (-len(blob) % 4))
        offset = len(blob)
        values = [v for row in rows for v in row]
        blob.extend(struct.pack("<" + "f" * len(values), *values))
        view = len(document.setdefault("bufferViews", []))
        document["bufferViews"].append({"buffer": 0, "byteOffset": offset, "byteLength": len(values) * 4})
        result = len(document.setdefault("accessors", []))
        spec = {"bufferView": view, "componentType": 5126, "count": len(rows), "type": kind}
        if kind == "SCALAR": spec.update(min=[min(values)], max=[max(values)])
        document["accessors"].append(spec)
        return result
    clip = {"name": "uv_flipbooks_v1", "samplers": [], "channels": []}
    # Supported frame counts/rates give power-of-two periods. Their common
    # loop period is the maximum, so short tracks repeat instead of freezing
    # after their first cycle while a longer track is still playing.
    clip_duration = max(track["duration_seconds"] for track in tracks)
    for track in tracks:
        matching = [(i, mat) for i, mat in enumerate(document["materials"])
                    if mat.get("extras", {}).get("b3d_uv_track") == track["id"]]
        if len(matching) != 1:
            raise ValueError("E_UV_ANIMATION_MATERIAL: " + track["id"])
        index, mat = matching[0]
        texture = mat["pbrMetallicRoughness"]["baseColorTexture"]
        texture.setdefault("extensions", {})["KHR_texture_transform"] = {"offset": [0., 0.]}
        offsets = track["offsets_gltf"]
        steps = round(clip_duration * track["fps"])
        if steps % len(offsets):
            raise ValueError("E_UV_COMMON_PERIOD")
        track["clip_duration_seconds"] = clip_duration
        times = [[i / track["fps"]] for i in range(steps + 1)]
        samples = [offsets[i % len(offsets)] for i in range(steps + 1)]
        sampler = len(clip["samplers"])
        clip["samplers"].append({"input": accessor(times, "SCALAR"),
                                  "output": accessor(samples, "VEC2"), "interpolation": "STEP"})
        pointer = f"/materials/{index}/pbrMetallicRoughness/baseColorTexture/extensions/KHR_texture_transform/offset"
        clip["channels"].append({"sampler": sampler, "target": {"path": "pointer", "extensions": {
            "KHR_animation_pointer": {"pointer": pointer}}}})
        track["material_index"] = index
        mat.setdefault("extras", {})["b3d_uv_animation"] = deepcopy(track)
    document.setdefault("animations", []).append(clip)
    for extension in ("KHR_texture_transform", "KHR_animation_pointer"):
        if extension not in document.setdefault("extensionsUsed", []): document["extensionsUsed"].append(extension)
    document["buffers"][0]["byteLength"] = len(blob)
    return bytes(blob), {"version": "1.0", "clip": "uv_flipbooks_v1", "duration_seconds": clip_duration, "tracks": tracks,
                         "requires": ["KHR_animation_pointer", "KHR_texture_transform"],
                         "fallback": "static first frame; loop playback is controlled by the host"}


def validate_pointer(document, reader, channel, sampler):
    """Independently check binary offsets, times, range and timeline agreement."""
    target = channel["target"]
    pointer = target.get("extensions", {}).get("KHR_animation_pointer", {}).get("pointer", "")
    match = re.fullmatch(r"/materials/(\d+)/pbrMetallicRoughness/baseColorTexture/extensions/KHR_texture_transform/offset", pointer)
    if not match or "node" in target:
        raise ValueError("E_GLB_UV_POINTER")
    if not {"KHR_texture_transform", "KHR_animation_pointer"} <= set(document.get("extensionsUsed", [])):
        raise ValueError("E_GLB_UV_EXTENSION")
    material = document["materials"][int(match[1])]
    transform = material["pbrMetallicRoughness"]["baseColorTexture"]["extensions"]["KHR_texture_transform"]
    if transform.get("offset") != [0, 0] or transform.get("scale", [1, 1]) != [1, 1] or transform.get("rotation", 0) != 0:
        raise ValueError("E_GLB_UV_BASE_TRANSFORM")
    track = material["extras"]["b3d_uv_animation"]
    if track.get("fps") not in (1, 2, 4, 8) or not 2 <= len(track.get("frames", [])) <= 4:
        raise ValueError("E_GLB_UV_TRACK")
    if len(track["offsets_gltf"]) != len(track["frames"]):
        raise ValueError("E_GLB_UV_FRAME_COUNT")
    times, values = reader.accessor(sampler["input"]), reader.accessor(sampler["output"])
    duration = track.get("clip_duration_seconds", track["duration_seconds"])
    if not math.isfinite(duration) or not 0 < duration <= 4:
        raise ValueError("E_GLB_UV_DURATION")
    step_count = duration * track["fps"]
    steps = round(step_count)
    if abs(steps - step_count) > 1e-6 or steps % len(track["frames"]):
        raise ValueError("E_GLB_UV_COMMON_PERIOD")
    expected = [track["offsets_gltf"][i % len(track["frames"])] for i in range(steps + 1)]
    if (sampler.get("interpolation") != "STEP" or len(times) != len(expected) or len(values) != len(expected)
        or reader.doc["accessors"][sampler["input"]]["componentType"] != 5126
        or reader.doc["accessors"][sampler["output"]]["componentType"] != 5126):
        raise ValueError("E_GLB_UV_SAMPLER")
    bounds = track["base_uv_bounds_gltf"]
    if len(bounds) != 4 or not all(math.isfinite(v) and 0 <= v <= 1 for v in bounds) or bounds[0] > bounds[2] or bounds[1] > bounds[3]:
        raise ValueError("E_GLB_UV_BOUNDS")
    used = False
    for mesh in document["meshes"]:
        for primitive in mesh["primitives"]:
            if primitive.get("material") != int(match[1]):
                continue
            used = True
            for uv in reader.accessor(primitive["attributes"]["TEXCOORD_0"]):
                if len(uv) != 2 or not (bounds[0] - 1e-6 <= uv[0] <= bounds[2] + 1e-6 and bounds[1] - 1e-6 <= uv[1] <= bounds[3] + 1e-6):
                    raise ValueError("E_GLB_UV_BASE_REGION")
    if not used:
        raise ValueError("E_GLB_UV_UNUSED_TRACK")
    for i, (time, offset, want) in enumerate(zip(times, values, expected)):
        if (len(time) != 1 or not math.isfinite(time[0]) or abs(time[0] - i / track["fps"]) > 1e-5
            or len(offset) != 2 or any(not math.isfinite(v) or abs(v - w) > 1e-6 for v, w in zip(offset, want))):
            raise ValueError("E_GLB_UV_KEYS")
        if not (0 <= bounds[0] + offset[0] <= bounds[2] + offset[0] <= 1
                and 0 <= bounds[1] + offset[1] <= bounds[3] + offset[1] <= 1):
            raise ValueError("E_GLB_UV_FRAME_BOUNDS")
    return len(times)

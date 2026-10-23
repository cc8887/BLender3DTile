"""GLB export and independent, binary-aware checks for the starter contract.

The inspector does not import Blender and does not replace Khronos' validator.
It checks embedded assets, UVs, actual weights, bind matrices and sampled CPU
skinning. All matrix calculations below use row-major Python lists internally;
glTF matrix arrays are converted from their column-major representation.
"""
from __future__ import annotations

from bisect import bisect_right
import json
import math
from pathlib import Path
import struct
from .rig_presets import RIG_IDS, skeleton


_JSON = 0x4E4F534A
_BIN = 0x004E4942
_COMPONENTS = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_BONE_NAMES = {"root", "pelvis", "spine", "chest", "neck", "head"} | {
    f"{part}_{side}" for part in ("clavicle", "upper_arm", "forearm", "hand", "thigh", "shin", "foot") for side in ("l", "r")
}


def _read_glb(path: Path) -> tuple[dict, bytes]:
    data = Path(path).read_bytes()
    if len(data) < 20:
        raise ValueError("GLB header/chunk is truncated")
    magic, version, length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise ValueError("GLB magic/version/declared length mismatch")
    chunks = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("Truncated chunk header")
        size, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        if size % 4 or offset + size > len(data):
            raise ValueError("Invalid chunk length or alignment")
        chunks.append((kind, data[offset:offset + size]))
        offset += size
    if not chunks or chunks[0][0] != _JSON or sum(kind == _JSON for kind, _ in chunks) != 1:
        raise ValueError("Exactly one JSON chunk must be first")
    if sum(kind == _BIN for kind, _ in chunks) != 1:
        raise ValueError("Starter GLB must contain exactly one BIN chunk")
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant {value}")
    document = json.loads(chunks[0][1].decode("utf-8"), parse_constant=reject_constant)
    if not isinstance(document, dict):
        raise ValueError("GLB JSON root must be an object")
    return document, next(chunk for kind, chunk in chunks if kind == _BIN)


def _write_glb(path: Path, document: dict, binary: bytes) -> None:
    encoded = json.dumps(document, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    binary += b"\0" * (-len(binary) % 4)
    data = (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(encoded) + 8 + len(binary))
            + struct.pack("<II", len(encoded), _JSON) + encoded
            + struct.pack("<II", len(binary), _BIN) + binary)
    Path(path).write_bytes(data)


def _index(values, index, label):
    if type(index) is not int or not 0 <= index < len(values):
        raise ValueError(f"{label} index out of range: {index}")
    return values[index]


class _Binary:
    def __init__(self, document: dict, binary: bytes):
        self.doc = document
        self.binary = binary
        self.cache = {}

    def view(self, index: int) -> memoryview:
        view = _index(self.doc.get("bufferViews", []), index, "bufferView")
        if view.get("buffer", 0) != 0:
            raise ValueError("Only the embedded buffer 0 is supported")
        start, size = view.get("byteOffset", 0), view["byteLength"]
        declared = self.doc.get("buffers", [{}])[0].get("byteLength", len(self.binary))
        if any(type(v) is not int for v in (start, size)) or start < 0 or size < 0 or start + size > min(len(self.binary), declared):
            raise ValueError("bufferView lies outside embedded buffer")
        return memoryview(self.binary)[start:start + size]

    @staticmethod
    def _layout(kind: str, component_size: int) -> tuple[list[int], int]:
        count = _COUNTS[kind]
        if kind.startswith("MAT"):
            dimension = int(kind[-1])
            column_bytes = dimension * component_size
            column_stride = (column_bytes + 3) & ~3
            return [column * column_stride + row * component_size for column in range(dimension) for row in range(dimension)], dimension * column_stride
        return [i * component_size for i in range(count)], count * component_size

    @staticmethod
    def _unpack(view, start, count, stride, component_type, offsets, item_size):
        code, size = _COMPONENTS[component_type]
        if any(type(v) is not int for v in (start, count, stride)) or min(start, count) < 0 or stride < item_size or stride % size:
            raise ValueError("Invalid accessor offset/count/stride")
        end = start + (count - 1) * stride + item_size if count else start
        if end > len(view):
            raise ValueError("Accessor exceeds bufferView")
        return [tuple(struct.unpack_from("<" + code, view, start + row * stride + offset)[0] for offset in offsets) for row in range(count)]

    def accessor(self, index: int) -> list[tuple]:
        if index in self.cache:
            return self.cache[index]
        accessor = _index(self.doc.get("accessors", []), index, "accessor")
        component_type, kind, count = accessor["componentType"], accessor["type"], accessor["count"]
        _, size = _COMPONENTS[component_type]
        offsets, item_size = self._layout(kind, size)
        if type(count) is not int or count < 0:
            raise ValueError("Invalid accessor count")
        if "bufferView" in accessor:
            view = self.view(accessor["bufferView"])
            spec = self.doc["bufferViews"][accessor["bufferView"]]
            values = self._unpack(view, accessor.get("byteOffset", 0), count, spec.get("byteStride", item_size), component_type, offsets, item_size)
        else:
            # A sparse accessor without a base buffer is initialized to zero.
            if count > max(1, len(self.binary)):
                raise ValueError("Unreasonably large implicit sparse accessor")
            values = [(0,) * len(offsets) for _ in range(count)]
        if "sparse" in accessor:
            sparse = accessor["sparse"]
            indices, replacement = sparse["indices"], sparse["values"]
            if indices["componentType"] not in (5121, 5123, 5125):
                raise ValueError("Invalid sparse index component type")
            index_size = _COMPONENTS[indices["componentType"]][1]
            idx = self._unpack(self.view(indices["bufferView"]), indices.get("byteOffset", 0), sparse["count"], index_size, indices["componentType"], [0], index_size)
            repl = self._unpack(self.view(replacement["bufferView"]), replacement.get("byteOffset", 0), sparse["count"], item_size, component_type, offsets, item_size)
            previous = -1
            for (destination,), value in zip(idx, repl):
                if not previous < destination < count:
                    raise ValueError("Sparse indices must be strictly increasing and in bounds")
                values[destination] = value
                previous = destination
        if accessor.get("normalized", False):
            if component_type == 5120:
                values = [tuple(max(-1, v / 127) for v in row) for row in values]
            elif component_type == 5122:
                values = [tuple(max(-1, v / 32767) for v in row) for row in values]
            elif component_type in (5121, 5123):
                divisor = 255 if component_type == 5121 else 65535
                values = [tuple(v / divisor for v in row) for row in values]
            else:
                raise ValueError("Unsupported normalized accessor component type")
        self.cache[index] = values
        return values


def _identity():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _multiply(a, b):
    return [[sum(a[row][k] * b[k][col] for k in range(4)) for col in range(4)] for row in range(4)]


def _matrix(values):
    if len(values) != 16:
        raise ValueError("Expected a 4x4 matrix")
    return [[values[col * 4 + row] for col in range(4)] for row in range(4)]


def _trs(node, overrides=None):
    if "matrix" in node and not overrides:
        return _matrix(node["matrix"])
    overrides = overrides or {}
    t = overrides.get("translation", node.get("translation", (0, 0, 0)))
    q = overrides.get("rotation", node.get("rotation", (0, 0, 0, 1)))
    s = overrides.get("scale", node.get("scale", (1, 1, 1)))
    if len(t) != 3 or len(q) != 4 or len(s) != 3 or not all(math.isfinite(v) for v in (*t, *q, *s)):
        raise ValueError("Non-finite or malformed node TRS")
    norm = math.sqrt(sum(v * v for v in q))
    if norm < 1e-12:
        raise ValueError("Zero quaternion")
    x, y, z, w = [v / norm for v in q]
    rotation = [[1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
                [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
                [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)]]
    result = _identity()
    for row in range(3):
        for col in range(3):
            result[row][col] = rotation[row][col] * s[col]
        result[row][3] = t[row]
    return result


def _world_matrices(nodes, overrides=None):
    parents = {}
    for i, node in enumerate(nodes):
        for child in node.get("children", []):
            _index(nodes, child, "child node")
            if child in parents:
                raise ValueError("Node has multiple parents")
            parents[child] = i
    result, visiting = {}, set()
    def visit(i):
        if i in result:
            return result[i]
        if i in visiting:
            raise ValueError("Node hierarchy has a cycle")
        visiting.add(i)
        local = _trs(nodes[i], (overrides or {}).get(i))
        result[i] = _multiply(visit(parents[i]), local) if i in parents else local
        visiting.remove(i)
        return result[i]
    for i in range(len(nodes)):
        visit(i)
    return result


def _transform(matrix, point):
    return tuple(sum(matrix[row][col] * point[col] for col in range(3)) + matrix[row][3] for row in range(3))


def _skin_points(positions, joints, weights, skin_matrices):
    result = []
    for point, joint_row, weight_row in zip(positions, joints, weights):
        output = [0., 0., 0.]
        for joint, weight in zip(joint_row, weight_row):
            if weight <= 0:
                continue
            transformed = _transform(skin_matrices[joint], point)
            for c in range(3):
                output[c] += transformed[c] * weight
        result.append(tuple(output))
    return result


def _interpolate(keys, values, time, interpolation, rotation=False):
    if not keys:
        raise ValueError("Empty animation sampler")
    times = [row[0] for row in keys]
    if len(values) != len(times) * (3 if interpolation == "CUBICSPLINE" else 1):
        raise ValueError("Animation input/output count mismatch")
    index = max(0, min(len(times) - 1, bisect_right(times, time) - 1))
    if interpolation == "CUBICSPLINE":
        if index == len(times) - 1 or time <= times[0]:
            value = values[index * 3 + 1]
        else:
            dt = times[index + 1] - times[index]
            alpha = (time - times[index]) / dt
            a2, a3 = alpha * alpha, alpha * alpha * alpha
            value = tuple((2*a3 - 3*a2 + 1) * a + (a3 - 2*a2 + alpha) * dt * at + (-2*a3 + 3*a2) * b + (a3 - a2) * dt * bt
                          for a, at, b, bt in zip(values[index*3 + 1], values[index*3 + 2], values[(index + 1)*3 + 1], values[(index + 1)*3]))
    elif interpolation == "STEP" or index == len(times) - 1 or time <= times[0]:
        value = values[index]
    elif interpolation == "LINEAR":
        alpha = (time - times[index]) / (times[index + 1] - times[index])
        a, b = values[index], values[index + 1]
        if rotation:
            dot = sum(x*y for x, y in zip(a, b))
            if dot < 0:
                b = tuple(-v for v in b)
                dot = -dot
            if dot < .9995:
                angle = math.acos(max(-1, min(1, dot)))
                denominator = math.sin(angle)
                value = tuple((math.sin((1-alpha)*angle)*x + math.sin(alpha*angle)*y) / denominator for x, y in zip(a, b))
            else:
                value = tuple(x*(1-alpha) + y*alpha for x, y in zip(a, b))
        else:
            value = tuple(x*(1-alpha) + y*alpha for x, y in zip(a, b))
    else:
        raise ValueError(f"Unsupported interpolation {interpolation}")
    if rotation:
        norm = math.sqrt(sum(v*v for v in value))
        if norm < 1e-12:
            raise ValueError("Animation contains zero quaternion")
        value = tuple(v/norm for v in value)
    return value


def inspect_glb(path: Path, require_skin: bool = False, require_animation: bool = False) -> dict:
    """Inspect actual binary content and sample exported skinning in pure Python."""
    errors = []
    metrics = {"inspector": "b3dtile_binary_v1", "official_validator": False}
    def error(code, **details):
        errors.append({"code": code, **details})
    try:
        doc, binary = _read_glb(path)
        reader = _Binary(doc, binary)
        if doc.get("asset", {}).get("version") != "2.0":
            error("E_GLB_VERSION")
        buffers = doc.get("buffers", [])
        if len(buffers) != 1 or any("uri" in b for b in buffers):
            error("E_GLB_EXTERNAL_BUFFER")
        if not buffers or not 0 <= len(binary) - buffers[0].get("byteLength", -1) <= 3:
            error("E_GLB_BUFFER_LENGTH")
        metrics.update(bytes=Path(path).stat().st_size, meshes=len(doc.get("meshes", [])),
                       nodes=len(doc.get("nodes", [])), skins=len(doc.get("skins", [])),
                       animations=len(doc.get("animations", [])), images=len(doc.get("images", [])),
                       materials=len(doc.get("materials", [])), primitives=0, vertices=0, triangles=0)
        if not doc.get("meshes"):
            error("E_GLB_NO_MESH")
        if not doc.get("images"):
            error("E_GLB_NO_IMAGE")
        for index, image in enumerate(doc.get("images", [])):
            if "uri" in image or "bufferView" not in image or image.get("mimeType") != "image/png":
                error("E_GLB_IMAGE_NOT_EMBEDDED_PNG", image=index)
                continue
            blob = reader.view(image["bufferView"])
            if bytes(blob[:8]) != b"\x89PNG\r\n\x1a\n":
                error("E_GLB_IMAGE_SIGNATURE", image=index)
        if not doc.get("samplers"):
            error("E_GLB_NO_SAMPLER")
        for index, sampler in enumerate(doc.get("samplers", [])):
            if any(sampler.get(key) != expected for key, expected in {"minFilter": 9728, "magFilter": 9728, "wrapS": 33071, "wrapT": 33071}.items()):
                error("E_GLB_PIXEL_SAMPLER", sampler=index)
        for texture in doc.get("textures", []):
            _index(doc.get("images", []), texture["source"], "texture image")
            _index(doc.get("samplers", []), texture["sampler"], "texture sampler")
        for index, material in enumerate(doc.get("materials", [])):
            reference = material.get("pbrMetallicRoughness", {}).get("baseColorTexture", {}).get("index")
            if reference is None:
                error("E_GLB_ATLAS_MATERIAL", material=index)
            else:
                _index(doc.get("textures", []), reference, "material texture")

        primitives = {}
        for mesh_index, mesh in enumerate(doc.get("meshes", [])):
            records = []
            for primitive_index, primitive in enumerate(mesh.get("primitives", [])):
                metrics["primitives"] += 1
                attributes = primitive["attributes"]
                positions = reader.accessor(attributes["POSITION"])
                metrics["vertices"] += len(positions)
                if not positions or any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in positions):
                    error("E_GLB_POSITION", mesh=mesh_index, primitive=primitive_index)
                if "TEXCOORD_0" not in attributes:
                    error("E_GLB_UV_MISSING", mesh=mesh_index, primitive=primitive_index)
                else:
                    uvs = reader.accessor(attributes["TEXCOORD_0"])
                    if len(uvs) != len(positions) or any(len(uv) != 2 or not all(math.isfinite(v) and 0 <= v <= 1 for v in uv) for uv in uvs):
                        error("E_GLB_UV_INVALID", mesh=mesh_index, primitive=primitive_index)
                if "COLOR_0" in attributes:
                    colors = reader.accessor(attributes["COLOR_0"])
                    metrics["colored_primitives"] = metrics.get("colored_primitives", 0) + 1
                    if len(colors) != len(positions) or any(len(c) not in (3, 4) or not all(math.isfinite(v) and 0 <= v <= 1 for v in c) for c in colors):
                        error("E_GLB_VERTEX_COLOR", mesh=mesh_index, primitive=primitive_index)
                if primitive.get("mode", 4) != 4:
                    error("E_GLB_NOT_TRIANGLES", mesh=mesh_index, primitive=primitive_index)
                if "indices" in primitive:
                    indices = reader.accessor(primitive["indices"])
                    if len(indices) % 3 or any(len(row) != 1 or type(row[0]) is not int or not 0 <= row[0] < len(positions) for row in indices):
                        error("E_GLB_INDEX_INVALID", mesh=mesh_index, primitive=primitive_index)
                    metrics["triangles"] += len(indices) // 3
                else:
                    metrics["triangles"] += len(positions) // 3
                _index(doc.get("materials", []), primitive["material"], "primitive material")
                records.append((primitive, positions))
            primitives[mesh_index] = records

        nodes = doc.get("nodes", [])
        worlds = _world_matrices(nodes)
        skins = doc.get("skins", [])
        if require_skin and not skins:
            error("E_GLB_SKIN_MISSING")
        if require_animation and not doc.get("animations"):
            error("E_GLB_ANIMATION_MISSING")
        skin_instances = []
        max_rest_error = max_weight_sum_error = 0.
        seam_duplicates = max_influences = 0
        used_skins = set()
        for node_index, node in enumerate(nodes):
            if "mesh" not in node:
                continue
            _index(doc.get("meshes", []), node["mesh"], "node mesh")
            if "skin" not in node:
                if require_skin:
                    error("E_GLB_MESH_SKIN_REFERENCE", node=node_index)
                continue
            used_skins.add(node["skin"])
            skin = _index(skins, node["skin"], "node skin")
            joint_nodes = skin["joints"]
            names = [_index(nodes, joint, "skin joint").get("name", "") for joint in joint_nodes]
            if len(joint_nodes) != len(set(joint_nodes)) or len(names) != len(set(names)) or not any(set(names) == set(skeleton(preset)) for preset in RIG_IDS):
                error("E_GLB_JOINT_SET", skin=node["skin"], count=len(joint_nodes), names=names)
            if "inverseBindMatrices" not in skin:
                error("E_GLB_INVERSE_BIND_MISSING", skin=node["skin"])
                continue
            inverse_rows = reader.accessor(skin["inverseBindMatrices"])
            if len(inverse_rows) != len(joint_nodes) or any(len(row) != 16 or not all(math.isfinite(v) for v in row) for row in inverse_rows):
                error("E_GLB_INVERSE_BIND_INVALID", skin=node["skin"])
                continue
            inverse = [_matrix(row) for row in inverse_rows]
            matrices = [_multiply(worlds[joint], bind) for joint, bind in zip(joint_nodes, inverse)]
            duplicates = {}
            for primitive_index, (primitive, positions) in enumerate(primitives[node["mesh"]]):
                attributes = primitive["attributes"]
                if "JOINTS_0" not in attributes or "WEIGHTS_0" not in attributes:
                    error("E_GLB_SKIN_ATTRIBUTES", node=node_index, primitive=primitive_index)
                    continue
                if any(name.startswith(("JOINTS_", "WEIGHTS_")) and name not in ("JOINTS_0", "WEIGHTS_0") for name in attributes):
                    error("E_GLB_TOO_MANY_INFLUENCES", node=node_index)
                joints, weights = reader.accessor(attributes["JOINTS_0"]), reader.accessor(attributes["WEIGHTS_0"])
                valid = len(joints) == len(weights) == len(positions)
                if not valid:
                    error("E_GLB_SKIN_COUNT", node=node_index)
                    continue
                for vertex_index, (position, joint_row, weight_row) in enumerate(zip(positions, joints, weights)):
                    if (len(joint_row) != 4 or len(weight_row) != 4
                        or any(type(joint) is not int or not 0 <= joint < len(joint_nodes) for joint in joint_row)
                        or any(not math.isfinite(weight) or weight < 0 or weight > 1 for weight in weight_row)):
                        error("E_GLB_SKIN_VALUES", node=node_index, vertex=vertex_index)
                        valid = False
                        break
                    total_error = abs(sum(weight_row) - 1)
                    max_weight_sum_error = max(max_weight_sum_error, total_error)
                    max_influences = max(max_influences, sum(weight > 1e-8 for weight in weight_row))
                    if total_error > 1e-4:
                        error("E_GLB_WEIGHT_SUM", node=node_index, vertex=vertex_index, actual=sum(weight_row))
                        valid = False
                        break
                    position_key = tuple(round(v, 7) for v in position)
                    influence_map = {}
                    for joint, weight in zip(joint_row, weight_row):
                        if weight > 1e-8:
                            influence_map[joint] = influence_map.get(joint, 0.) + weight
                    if position_key in duplicates:
                        seam_duplicates += 1
                        old = duplicates[position_key]
                        if any(abs(old.get(key, 0) - influence_map.get(key, 0)) > 1e-5 for key in old.keys() | influence_map.keys()):
                            error("E_GLB_SEAM_WEIGHTS", node=node_index, vertex=vertex_index)
                    else:
                        duplicates[position_key] = influence_map
                if not valid:
                    continue
                reconstructed = _skin_points(positions, joints, weights, matrices)
                # Starter's pinned Blender 4.5 exporter bakes mesh.matrix_world
                # and the Y-up conversion into skinned POSITION data. Its IBMs
                # invert world-space rest joints, so the bind-pose reference is
                # the raw POSITION, already in world coordinates. glTF ignores
                # the skinned mesh node transform: applying it here would double
                # placement/rotation/scale. This rest-reference convention is a
                # contract of our exporter, not an assumption for arbitrary glTF
                # assets (which may bake a bind-shape matrix into their IBMs).
                # https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#joint-hierarchy
                expected = positions
                rest_error = max((math.dist(a, b) for a, b in zip(reconstructed, expected)), default=0.)
                max_rest_error = max(max_rest_error, rest_error)
                if rest_error > 1e-5:
                    error("E_GLB_REST_RECONSTRUCTION", node=node_index, max_error_m=rest_error)
                skin_instances.append({"node": node_index, "positions": positions, "joints": joints, "weights": weights,
                                       "joint_nodes": joint_nodes, "inverse": inverse, "rest": reconstructed})
        if skins and len(used_skins) != len(skins):
            error("E_GLB_UNUSED_SKIN")
        metrics.update(max_rest_error_m=max_rest_error, max_weight_sum_error=max_weight_sum_error,
                       max_influences=max_influences, seam_duplicate_vertices=seam_duplicates,
                       sampled_skin_poses=0, animation_max_displacement_m=0.)
        for animation_index, animation in enumerate(doc.get("animations", [])):
            channels = []
            key_times = set()
            pointer_channels = 0
            pointers = set()
            for channel in animation.get("channels", []):
                target = channel["target"]
                if target.get("path") == "pointer":
                    from .uv_animation import validate_pointer
                    pointer = target.get("extensions", {}).get("KHR_animation_pointer", {}).get("pointer")
                    if pointer in pointers:
                        error("E_GLB_DUPLICATE_POINTER")
                    pointers.add(pointer)
                    sampler = _index(animation["samplers"], channel["sampler"], "animation sampler")
                    try:
                        metrics["uv_animation_keys"] = metrics.get("uv_animation_keys", 0) + validate_pointer(doc, reader, channel, sampler)
                        pointer_channels += 1
                        metrics["uv_animation_channels"] = metrics.get("uv_animation_channels", 0) + 1
                    except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as exc:
                        error("E_GLB_UV_ANIMATION", detail=str(exc))
                    continue
                target_node = target["node"]
                _index(nodes, target_node, "animation target")
                path_name = target["path"]
                if path_name not in ("translation", "rotation", "scale"):
                    error("E_GLB_ANIMATION_PATH", path=path_name)
                    continue
                sampler = _index(animation["samplers"], channel["sampler"], "animation sampler")
                keys, values = reader.accessor(sampler["input"]), reader.accessor(sampler["output"])
                if (not keys or any(len(key) != 1 or not math.isfinite(key[0]) or key[0] < 0 for key in keys)
                    or any(b[0] <= a[0] for a, b in zip(keys, keys[1:]))
                    or any(not all(math.isfinite(value) for value in row) for row in values)):
                    error("E_GLB_ANIMATION_DATA", animation=animation_index)
                    continue
                if "matrix" in nodes[target_node]:
                    error("E_GLB_ANIMATED_MATRIX_NODE", node=target_node)
                key_times.update(key[0] for key in keys)
                channels.append((target_node, path_name, keys, values, sampler.get("interpolation", "LINEAR")))
            if not channels:
                if not pointer_channels:
                    error("E_GLB_EMPTY_ANIMATION", animation=animation_index)
                continue
            times = sorted(key_times)
            samples = sorted(set(times + [(a+b)/2 for a, b in zip(times, times[1:])]))
            # Fixed smoke clips are short. Sampling every exported key and the
            # midpoint also checks interpolation, not just accessor presence.
            for time in samples:
                overrides = {}
                for target_node, path_name, keys, values, interpolation in channels:
                    overrides.setdefault(target_node, {})[path_name] = _interpolate(keys, values, time, interpolation, path_name == "rotation")
                animated_worlds = _world_matrices(nodes, overrides)
                for instance in skin_instances:
                    matrices = [_multiply(animated_worlds[joint], bind) for joint, bind in zip(instance["joint_nodes"], instance["inverse"])]
                    points = _skin_points(instance["positions"], instance["joints"], instance["weights"], matrices)
                    if not all(math.isfinite(v) for point in points for v in point):
                        error("E_GLB_POSE_NONFINITE", animation=animation_index, time=time, node=instance["node"])
                        continue
                    displacement = max((math.dist(a, b) for a, b in zip(points, instance["rest"])), default=0.)
                    metrics["animation_max_displacement_m"] = max(metrics["animation_max_displacement_m"], displacement)
                    extent = math.dist(tuple(min(p[c] for p in instance["rest"]) for c in range(3)), tuple(max(p[c] for p in instance["rest"]) for c in range(3)))
                    if displacement > max(1., extent) * 10:
                        error("E_GLB_POSE_EXPLODED", animation=animation_index, time=time, displacement_m=displacement)
                metrics["sampled_skin_poses"] += bool(skin_instances)
        if require_animation and skin_instances and metrics["animation_max_displacement_m"] < 1e-5:
            error("E_GLB_ANIMATION_NO_DEFORMATION")
    except (OSError, ValueError, KeyError, IndexError, TypeError, struct.error, OverflowError) as exc:
        error("E_GLB_INVALID", detail=str(exc))
    return {"ok": not errors, "errors": errors, "metrics": metrics}


def export_glb(instances: dict, filepath: Path, include_animation: bool) -> dict:
    """Export only asset objects and normalize the pixel sampling contract."""
    import bpy
    filepath = Path(filepath).resolve()
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    selected = []
    for instance in instances.values():
        for obj in (instance.root, instance.mesh, instance.rig):
            if obj is not None and obj not in selected:
                obj.hide_set(False)
                obj.select_set(True)
                selected.append(obj)
    if not selected:
        raise ValueError("E_EXPORT_EMPTY_ASSET")
    bpy.context.view_layer.objects.active = selected[0]
    bpy.context.scene.frame_set(bpy.context.scene.frame_start)
    bpy.context.view_layer.update()
    result = bpy.ops.export_scene.gltf(
        filepath=str(filepath), export_format="GLB", use_selection=True,
        export_texcoords=True, export_normals=True, export_materials="EXPORT",
        export_image_format="AUTO", export_yup=True, export_apply=False,
        export_skins=True, export_influence_nb=4, export_all_influences=False,
        export_def_bones=False, export_leaf_bone=False,
        export_armature_object_remove=False, export_hierarchy_flatten_bones=False,
        export_animations=include_animation, export_animation_mode="ACTIVE_ACTIONS",
        export_rest_position_armature=True, export_reset_pose_bones=True,
        export_force_sampling=True, export_optimize_animation_size=False,
        export_cameras=False, export_lights=False,
        export_vertex_color="ACTIVE", export_all_vertex_colors=False, export_extras=True,
    )
    if "FINISHED" not in result:
        raise RuntimeError(f"E_EXPORT_FAILED: {result}")
    document, binary = _read_glb(filepath)
    for sampler in document.get("samplers", []):
        sampler.update(minFilter=9728, magFilter=9728, wrapS=33071, wrapT=33071)
    from .uv_animation import append_to_glb
    binary, timeline = append_to_glb(document, binary, instances)
    if timeline["tracks"]:
        (filepath.parent / "uv_animation.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_glb(filepath, document, binary)
    report = inspect_glb(filepath, require_skin=any(i.rig is not None for i in instances.values()), require_animation=include_animation)
    report["path"] = str(filepath)
    return report

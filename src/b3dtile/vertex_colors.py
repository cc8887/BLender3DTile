"""Region-addressed color attributes; palette sRGB is converted to linear RGBA.

CORNER storage keeps semantic seams independent. glTF splits those corners
into vertices and emits COLOR_0. The material multiplies atlas RGB by COLOR_0.
"""
import math
from .atlas import _palette

ATTRIBUTE = "B3DColor"


def _linear(value):
    c = value / 255.
    return c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4


def paint_vertex_color(instance, region, style, palette, color_a, color_b, catalog):
    mesh = instance.mesh.data
    if region not in instance.regions or style not in ("solid", "height_gradient"):
        raise ValueError("E_VERTEX_COLOR_REGION_OR_STYLE")
    attr = mesh.color_attributes.get(ATTRIBUTE)
    if attr is None:
        attr = mesh.color_attributes.new(name=ATTRIBUTE, type="FLOAT_COLOR", domain="CORNER")
        for datum in attr.data:
            datum.color = (1, 1, 1, 1)
    mesh.color_attributes.active_color = attr
    colors = _palette(catalog, palette)
    first, second = [tuple(_linear(c) for c in colors[index]) for index in (color_a, color_b)]
    loops = [li for pi in instance.regions[region] for li in mesh.polygons[pi].loop_indices]
    heights = [mesh.vertices[mesh.loops[li].vertex_index].co.z for li in loops]
    lo, hi = min(heights), max(heights)
    for li, height in zip(loops, heights):
        t = (height - lo) / (hi - lo) if style == "height_gradient" and hi > lo else 0.
        attr.data[li].color = (*[a + (b - a) * t for a, b in zip(first, second)], 1.)
    instance.meta.setdefault("vertex_colors", {})[region] = {
        "attribute": ATTRIBUTE, "style": style, "palette": palette,
        "color_a": color_a, "color_b": color_b, "blend": "multiply_atlas", "space": "linear"}
    ensure_materials(instance)
    mesh.update()


def ensure_materials(instance):
    """Reapply after slot changes so preview and exported COLOR_0 agree."""
    mesh = instance.mesh.data
    if not mesh.color_attributes.get(ATTRIBUTE):
        return
    # Change only this object's material copies; unpainted meshes stay unchanged.
    for index, material in enumerate(list(mesh.materials)):
        if material.get("b3d_vertex_color") == ATTRIBUTE:
            continue
        material = material.copy()
        material.name += "_" + instance.object_id + "_color"
        nodes, links = material.node_tree.nodes, material.node_tree.links
        shader = nodes.get("Principled BSDF")
        source = shader.inputs["Base Color"].links[0].from_socket
        vertex = nodes.new("ShaderNodeVertexColor")
        vertex.layer_name = ATTRIBUTE
        mix = nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.inputs[0].default_value = 1
        links.new(source, mix.inputs[1])
        links.new(vertex.outputs["Color"], mix.inputs[2])
        links.new(mix.outputs["Color"], shader.inputs["Base Color"])
        material["b3d_vertex_color"] = ATTRIBUTE
        mesh.materials[index] = material


def validate_vertex_colors(instance):
    attr = instance.mesh.data.color_attributes.get(ATTRIBUTE)
    if attr is None:
        return {"ok": not instance.meta.get("vertex_colors"), "present": False, "errors": []}
    errors = []
    if attr.domain != "CORNER" or len(attr.data) != len(instance.mesh.data.loops):
        errors.append("E_VERTEX_COLOR_DOMAIN")
    if any(not all(math.isfinite(v) and 0 <= v <= 1 for v in d.color) for d in attr.data):
        errors.append("E_VERTEX_COLOR_VALUES")
    return {"ok": not errors, "present": True, "attribute": ATTRIBUTE,
            "domain": attr.domain, "corners": len(attr.data), "errors": errors}

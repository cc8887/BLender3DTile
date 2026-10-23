"""Project-authored, fixed game humanoids; not UE/Mixamo skeleton identities.

Every preset uses the certified humanoid_round_v1 surface/rest frame. Reduced
rigs merge canonical influences; they never reuse vertex IDs from another mesh.
This module is also readable without importing bpy.
"""
from .geometry import humanoid_bones


RIG_IDS = ("humanoid_20_v1", "humanoid_compact_16_v1", "humanoid_basic_12_v1")
WEIGHT_IDS = ("anatomical_smooth_v1", "anatomical_crisp_v1")


def bone_mapping(preset):
    if preset not in RIG_IDS:
        raise ValueError("E_RIG_UNKNOWN: " + preset)
    mapping = {name: name for name in humanoid_bones()}
    if preset != RIG_IDS[0]:
        mapping.update(chest="spine", neck="head", clavicle_l="spine", clavicle_r="spine")
    if preset == RIG_IDS[2]:
        for side in ("l", "r"):
            mapping["hand_" + side] = "forearm_" + side
            mapping["foot_" + side] = "shin_" + side
    return mapping


def skeleton(preset):
    source = humanoid_bones()
    mapping = bone_mapping(preset)
    bones = {}
    for name, (head, tail, parent) in source.items():
        if mapping[name] != name:
            continue
        if preset != RIG_IDS[0]:
            if name == "spine": tail = source["chest"][1]
            if name == "head": head = source["neck"][0]
        if preset == RIG_IDS[2]:
            for side in ("l", "r"):
                if name == "forearm_" + side: tail = source["hand_" + side][1]
                if name == "shin_" + side: tail = source["foot_" + side][1]
        bones[name] = (head, tail, mapping[parent] if parent else None)
    return bones


def remap_weights(weights, preset, weight_preset):
    if weight_preset not in WEIGHT_IDS:
        raise ValueError("E_WEIGHT_PRESET: " + weight_preset)
    mapping = bone_mapping(preset)
    merged = {}
    for bone, weight in weights.items():
        name = mapping[bone]
        merged[name] = merged.get(name, 0.) + weight
    # A bounded, deterministic sharper blend, still normalized and deformable.
    if weight_preset == "anatomical_crisp_v1":
        merged = {name: value ** 1.35 for name, value in merged.items()}
    total = sum(merged.values())
    return {name: value / total for name, value in merged.items() if value > 0}


def catalog_presets():
    return [{"id": preset, "compatible_kits": ["humanoid_round_v1"],
             "provenance": "original project-authored canonical A-pose; no third-party rig data",
             "bone_count": len(skeleton(preset)), "max_influences": 4,
             "weight_presets": list(WEIGHT_IDS), "default_weight_preset": WEIGHT_IDS[0],
             "weight_method": "Topology-locked anatomical envelopes, merged into this skeleton and normalized",
             "bones": [{"name": n, "head": h, "tail": t, "parent": p} for n, (h, t, p) in skeleton(preset).items()]}
            for preset in RIG_IDS]

"""Geometry regression checks that require the pinned bpy wheel."""
import json
import math
from collections import Counter
from pathlib import Path

import pytest

bpy=pytest.importorskip("bpy")
from b3dtile.geometry import build_kit,topology_signature
from b3dtile.rig import bind_humanoid,validate_rig,create_smoke_animation

pytestmark=pytest.mark.blender


@pytest.fixture
def catalog():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return json.loads((Path(__file__).parents[1]/"catalog/starter_v1.catalog.json").read_text())


def test_all_kits_have_complete_finite_regions_and_uvs(catalog):
    for spec in catalog["kits"]:
        kit=build_kit(spec["id"],spec["id"],catalog)
        mesh=kit.mesh.data
        assert mesh.vertices and mesh.polygons
        assert set(kit.regions)==set(spec["regions"])
        assert all(kit.regions.values()), spec["id"]
        assert sorted(p for values in kit.regions.values() for p in values)==list(range(len(mesh.polygons)))
        assert set(kit.uv_template)==set(range(len(mesh.loops)))
        assert all(math.isfinite(c) and 0<=c<=1 for uv in kit.uv_template.values() for c in uv)
        assert min(p.area for p in mesh.polygons)>1e-8
        # Validate a copy so an invalid source cannot silently repair itself
        # and shift the region/UV indices before the glTF exporter sees it.
        validation_copy=mesh.copy()
        try:
            assert validation_copy.validate() is False,spec["id"]
            assert topology_signature(validation_copy)==topology_signature(mesh)
        finally:
            bpy.data.meshes.remove(validation_copy)


def test_roof_is_true_35_degree_with_outward_gables(catalog):
    kit=build_kit("roof_gable_35_v1","roof",catalog)
    for region in ["roof_left","roof_right"]:
        for index in kit.regions[region]:
            normal=kit.mesh.data.polygons[index].normal
            assert abs(math.degrees(math.acos(normal.z))-35)<1e-4
    assert kit.mesh.data.polygons[kit.regions["gable_front"][0]].normal.y<-.99
    assert kit.mesh.data.polygons[kit.regions["gable_back"][0]].normal.y>.99


def test_humanoid_is_single_closed_surface_and_reproducible(catalog):
    kit=build_kit("humanoid_round_v1","human",catalog)
    mesh=kit.mesh.data
    counts=Counter(tuple(sorted(e)) for p in mesh.polygons for e in p.edge_keys)
    assert set(counts.values())=={2}
    graph={i:set() for i in range(len(mesh.vertices))}
    for a,b in counts:graph[a].add(b);graph[b].add(a)
    seen=set();stack=[0]
    while stack:
        vertex=stack.pop()
        if vertex in seen:continue
        seen.add(vertex);stack.extend(graph[vertex]-seen)
    assert len(seen)==len(mesh.vertices)
    second=build_kit("humanoid_round_v1","human2",catalog)
    assert topology_signature(mesh)==topology_signature(second.mesh.data)


def test_bound_humanoid_pose_regression_and_mutation_rejection(catalog):
    kit=build_kit("humanoid_round_v1","human",catalog)
    bind_humanoid(kit,"humanoid_20_v1",catalog)
    report=validate_rig(kit)
    assert report["ok"],report
    assert report["metrics"]["bones"]==20
    assert set(report["metrics"]["poses"])=={"rest","arms_raise_90","arms_raise_120","elbows_bend_90"}
    create_smoke_animation(kit)
    assert kit.rig.animation_data.action.name=="rig_smoke_v1"
    second=build_kit("humanoid_round_v1","modified",catalog)
    second.mesh.data.vertices[0].co.x+=.01
    with pytest.raises(ValueError,match="E_TOPOLOGY_CHANGED"):
        bind_humanoid(second,"humanoid_20_v1",catalog)


def test_armature_parents_mesh_and_preserves_transformed_world_placement(catalog):
    kit=build_kit("humanoid_round_v1","human",catalog)
    kit.root.location=(1.25,-2,.5)
    kit.root.rotation_euler.z=math.pi/2
    kit.root.scale=(2,2,2)
    bpy.context.view_layer.update()
    before=kit.mesh.matrix_world.copy()
    signature=topology_signature(kit.mesh.data)
    bind_humanoid(kit,"humanoid_20_v1",catalog)
    assert kit.mesh.parent==kit.rig
    assert kit.rig.parent==kit.root
    assert max(abs(a-b) for ra,rb in zip(before,kit.mesh.matrix_world) for a,b in zip(ra,rb))<1e-6
    assert topology_signature(kit.mesh.data)==signature
    assert validate_rig(kit)["ok"]


def test_character_charts_sharing_slots_do_not_overlap(catalog):
    kit=build_kit("humanoid_round_v1","human",catalog)
    spec=next(k for k in catalog["kits"] if k["id"]==kit.kit_id)
    rectangles=kit.meta["region_chart_rects"]
    for a,sa in spec["regions"].items():
        for b,sb in spec["regions"].items():
            if a>=b or not (set(sa["allowed_slots"])&set(sb["allowed_slots"])):continue
            x,y,w,h=rectangles[a];X,Y,W,H=rectangles[b]
            assert x+w<=X or X+W<=x or y+h<=Y or Y+H<=y,(a,b)

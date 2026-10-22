"""Fixed humanoid skeleton, deterministic weights and pose regression checks."""
from __future__ import annotations
import math

from .geometry import humanoid_bones, topology_signature, _segment_distance


def _weights(co,bones):
    from mathutils import Vector
    x,y,z=co;side="l" if x>=0 else "r"
    # Anatomical envelopes prevent a nearby hanging hand from binding a thigh.
    if z>1.23:
        return {"head":1.}
    if z>1.12 and abs(x)<.16:
        t=max(0.,min(1.,(z-1.13)/.13));return {"neck":1-t,"head":t}
    if abs(x)>.23 and z>.56:
        names=["clavicle_"+side,"upper_arm_"+side,"forearm_"+side,"hand_"+side]
        if abs(x)<.32: names+=["chest"]
    elif z<.70:
        names=["pelvis","thigh_"+side,"shin_"+side,"foot_"+side]
    else:
        names=["pelvis","spine","chest","neck"]
        if abs(x)>.15 and z>.94: names+=["clavicle_"+side,"upper_arm_"+side]
    ds=sorted([(_segment_distance(co,*bones[n][:2])[0],n) for n in names])[:3]
    minimum=ds[0][0]
    vals={n: math.exp(-((d-minimum)/.075)**2)/(d+.045)**2 for d,n in ds}
    vals={n:w for n,w in vals.items() if w>max(vals.values())*.008}
    total=sum(vals.values())
    return {n:w/total for n,w in vals.items()}


def bind_humanoid(instance,preset,catalog)->dict:
    import bpy
    if instance.kit_id!="humanoid_round_v1" or preset!="humanoid_20_v1":
        raise ValueError("E_RIG_INCOMPATIBLE: fixed template required")
    if instance.rig is not None: raise ValueError("E_ALREADY_BOUND")
    if topology_signature(instance.mesh.data)!=instance.meta["topology_sha256"]:
        raise ValueError("E_TOPOLOGY_CHANGED")
    kit_spec=next(k for k in catalog["kits"] if k["id"]==instance.kit_id)
    if kit_spec.get("topology_sha256") and instance.meta["topology_sha256"]!=kit_spec["topology_sha256"]:
        raise ValueError("E_TOPOLOGY_CATALOG_MISMATCH")
    bones=humanoid_bones()
    spec=next(p for p in catalog["rig_presets"] if p["id"]==preset)
    if set(bones)!={b["name"] for b in spec["bones"]}: raise ValueError("E_RIG_CATALOG_MISMATCH")
    arm_data=bpy.data.armatures.new(instance.object_id+"_skeleton")
    arm=bpy.data.objects.new(instance.object_id+"_rig",arm_data)
    bpy.context.collection.objects.link(arm);arm.parent=instance.root
    bpy.ops.object.select_all(action="DESELECT");arm.select_set(True);bpy.context.view_layer.objects.active=arm
    bpy.ops.object.mode_set(mode="EDIT")
    for name,(head,tail,parent) in bones.items():
        bone=arm_data.edit_bones.new(name);bone.head=head;bone.tail=tail
        bone.use_deform=True
        if parent: bone.parent=arm_data.edit_bones[parent]
        # Roll is aligned explicitly so poses can use rest-matrix axes.
        bone.align_roll((0,1,0))
    bpy.ops.object.mode_set(mode="OBJECT")
    groups={n:instance.mesh.vertex_groups.new(name=n) for n in bones}
    table=[]
    for v in instance.mesh.data.vertices:
        weights=_weights(v.co,bones);table.append(weights)
        for name,value in weights.items():
            if value>1e-9: groups[name].add([v.index],value,"REPLACE")
    modifier=instance.mesh.modifiers.new("humanoid_20_v1 linear skin","ARMATURE")
    modifier.object=arm;modifier.use_deform_preserve_volume=False
    # glTF resolves a skinned mesh through its ancestor armature. Keeping the
    # armature and mesh as siblings forces exporter heuristics and can select
    # the wrong rig in a multi-object scene. Preserve the placed world frame.
    bpy.context.view_layer.update()
    mesh_world=instance.mesh.matrix_world.copy()
    instance.mesh.parent=arm
    instance.mesh.matrix_world=mesh_world
    instance.rig=arm;instance.meta["weight_table"]=table;instance.meta["rig_preset"]=preset
    arm.show_in_front=True
    bpy.context.view_layer.update()
    return {"preset":preset,"bones":len(bones),"vertices":len(table),"max_influences":max(map(len,table))}


def set_pose(instance,pose)->None:
    import bpy
    from mathutils import Vector,Quaternion
    if pose not in ["rest","arms_raise_90","arms_raise_120","elbows_bend_90"]:
        raise ValueError("E_UNKNOWN_POSE: "+pose)
    if instance.rig is None:
        if pose=="rest": return
        raise ValueError("E_POSE_REQUIRES_RIG")
    arm=instance.rig
    for bone in arm.pose.bones:
        bone.rotation_mode="QUATERNION";bone.rotation_quaternion=(1,0,0,0);bone.location=(0,0,0);bone.scale=(1,1,1)
    for side,sgn in [("l",1),("r",-1)]:
        if pose.startswith("arms_raise"):
            target=90 if pose.endswith("90") else 120
            bone=arm.pose.bones["upper_arm_"+side]
            rest_direction=bone.bone.tail_local-bone.bone.head_local
            rest_elevation=math.atan2(rest_direction.z,abs(rest_direction.x))
            target_elevation=math.radians(target-90)
            angle=-(target_elevation-rest_elevation)*sgn
            axis=bone.bone.matrix_local.to_3x3().inverted()@Vector((0,1,0))
            bone.rotation_quaternion=Quaternion(axis,angle)
        elif pose=="elbows_bend_90":
            bone=arm.pose.bones["forearm_"+side]
            axis=bone.bone.matrix_local.to_3x3().inverted()@Vector((0,0,1))
            bone.rotation_quaternion=Quaternion(axis,-sgn*math.pi/2)
    bpy.context.view_layer.update()


def _evaluated_vertices(instance):
    import bpy
    dg=bpy.context.evaluated_depsgraph_get();obj=instance.mesh.evaluated_get(dg)
    evaluated=obj.to_mesh()
    try:return [v.co.copy() for v in evaluated.vertices]
    finally:obj.to_mesh_clear()


def validate_rig(instance)->dict:
    if instance.rig is None:return {"ok":False,"errors":["E_RIG_MISSING"],"metrics":{}}
    mesh=instance.mesh.data;errors=[];metrics={}
    if topology_signature(mesh)!=instance.meta["topology_sha256"]:errors.append("E_TOPOLOGY_CHANGED")
    max_influences=0;max_sum_error=0.
    for v in mesh.vertices:
        weights=[g.weight for g in v.groups if g.weight>1e-8]
        max_influences=max(max_influences,len(weights));total=sum(weights)
        max_sum_error=max(max_sum_error,abs(total-1))
        if not weights or any(not math.isfinite(w) or w<0 for w in weights):errors.append("E_INVALID_WEIGHTS");break
    if max_influences>4:errors.append("E_TOO_MANY_WEIGHTS")
    if max_sum_error>1e-4:errors.append("E_WEIGHT_SUM")
    metrics.update(max_influences=max_influences,max_weight_sum_error=max_sum_error,bones=len(instance.rig.data.bones))
    original=[v.co.copy() for v in mesh.vertices]
    mesh.calc_loop_triangles();triangles=[tuple(t.vertices) for t in mesh.loop_triangles]
    base_areas=[(original[b]-original[a]).cross(original[c]-original[a]).length/2 for a,b,c in triangles]
    base_lengths=[(original[e.vertices[0]]-original[e.vertices[1]]).length for e in mesh.edges]
    metrics["poses"]={}
    try:
        for pose in ["rest","arms_raise_90","arms_raise_120","elbows_bend_90"]:
            set_pose(instance,pose);vs=_evaluated_vertices(instance)
            finite=all(math.isfinite(c) for v in vs for c in v)
            ratios=sorted((vs[e.vertices[0]]-vs[e.vertices[1]]).length/max(length,1e-10) for e,length in zip(mesh.edges,base_lengths))
            area_ratios=[(vs[b]-vs[a]).cross(vs[c]-vs[a]).length/2/max(area,1e-12) for (a,b,c),area in zip(triangles,base_areas)]
            record={"finite":finite,"edge_stretch_max":max(ratios),"edge_stretch_p99":ratios[int(.99*(len(ratios)-1))],
                    "edge_length_ratio_min":min(ratios),"triangle_area_ratio_min":min(area_ratios),
                    "degenerate_triangles":sum(r<1e-4 for r in area_ratios)}
            if pose=="rest":
                record["rest_error_m"]=max((a-b).length for a,b in zip(original,vs))
                if record["rest_error_m"]>1e-5:errors.append("E_REST_RECONSTRUCTION")
            if not finite:errors.append("E_POSE_NONFINITE:"+pose)
            if record["edge_stretch_max"]>3 or record["edge_stretch_p99"]>2:errors.append("E_POSE_STRETCH:"+pose)
            if record["degenerate_triangles"]:errors.append("E_POSE_DEGENERATE:"+pose)
            metrics["poses"][pose]=record
    finally:set_pose(instance,"rest")
    return {"ok":not errors,"errors":errors,"metrics":metrics}


def create_smoke_animation(instance)->None:
    import bpy
    if instance.rig is None:raise ValueError("E_ANIMATION_REQUIRES_RIG")
    for frame,pose in [(1,"rest"),(13,"arms_raise_90"),(25,"arms_raise_120"),(37,"elbows_bend_90"),(49,"rest")]:
        set_pose(instance,pose)
        for bone in instance.rig.pose.bones:
            bone.keyframe_insert(data_path="rotation_quaternion",frame=frame,group=bone.name)
    instance.rig.animation_data.action.name="rig_smoke_v1"
    bpy.context.scene.frame_start=1;bpy.context.scene.frame_end=49;bpy.context.scene.render.fps=24
    bpy.context.scene.frame_set(1);set_pose(instance,"rest")

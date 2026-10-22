"""Trusted, deterministic kit geometry. No recipe text is ever executed.

Coordinates are metres, Z up and -Y forward. UV templates use a top-left
origin; atlas.py owns conversion into Blender texture coordinates.
"""
from __future__ import annotations

import hashlib
import json
import math

from .model import KitInstance


def topology_signature(mesh):
    payload = {
        "vertices": [[round(float(c), 7) for c in v.co] for v in mesh.vertices],
        "faces": [list(p.vertices) for p in mesh.polygons],
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


class _Builder:
    def __init__(self):
        self.vertices, self.faces, self.labels, self.uvs = [], [], [], []
        self.lookup = {}

    def vertex(self, co):
        key = tuple(round(float(x), 8) for x in co)
        if key not in self.lookup:
            self.lookup[key] = len(self.vertices)
            self.vertices.append(key)
        return self.lookup[key]

    def face(self, points, region, uv=None):
        indices = [self.vertex(p) for p in points]
        if len(set(indices)) < 3:
            return
        self.faces.append(indices)
        self.labels.append(region)
        if uv is None:
            # Planar projection for caps, normalized as one continuous chart.
            spans = [max(p[i] for p in points) - min(p[i] for p in points) for i in range(3)]
            axes = sorted(range(3), key=lambda i: spans[i], reverse=True)[:2]
            lo = [min(p[i] for p in points) for i in axes]
            uv = [tuple((p[a] - lo[j]) / max(spans[a], 1e-9) for j, a in enumerate(axes)) for p in points]
        self.uvs.append(uv)

    def quad(self, corners, region, grid=True):
        from mathutils import Vector
        a, b, c, d = map(Vector, corners)
        nx = max(1, math.ceil((b-a).length / .5 - 1e-7)) if grid else 1
        ny = max(1, math.ceil((d-a).length / .5 - 1e-7)) if grid else 1
        def p(u, v):
            return a*(1-u)*(1-v) + b*u*(1-v) + c*u*v + d*(1-u)*v
        for j in range(ny):
            for i in range(nx):
                self.face([p(i/nx, j/ny), p((i+1)/nx, j/ny),
                           p((i+1)/nx, (j+1)/ny), p(i/nx, (j+1)/ny)],
                          region, [(0, 1), (1, 1), (1, 0), (0, 0)])

    def box(self, lo, hi, region, cap=None):
        x,y,z=lo; X,Y,Z=hi
        for pts in [[(x,y,z),(x,y,Z),(x,Y,Z),(x,Y,z)],
                    [(X,y,z),(X,Y,z),(X,Y,Z),(X,y,Z)],
                    [(x,y,z),(X,y,z),(X,y,Z),(x,y,Z)],
                    [(x,Y,z),(x,Y,Z),(X,Y,Z),(X,Y,z)]]:
            self.quad(pts, region)
        self.quad([(x,y,z),(x,Y,z),(X,Y,z),(X,y,z)], cap or region)
        self.quad([(x,y,Z),(X,y,Z),(X,Y,Z),(x,Y,Z)], cap or region)

    def extrude_xz(self, outline, y0, y1, region, edge_region=None):
        # Outline is counterclockwise viewed from -Y.
        self.face([(x,y0,z) for x,z in outline], region)
        self.face([(x,y1,z) for x,z in reversed(outline)], region)
        for a,b in zip(outline, outline[1:]+outline[:1]):
            self.quad([(a[0],y0,a[1]),(a[0],y1,a[1]),(b[0],y1,b[1]),(b[0],y0,b[1])], edge_region or region)

    def finish(self, object_id, kit_id, spec):
        import bpy
        # Joined wall/frame solids share vertices. Remove their opposing
        # internal boundary faces *before* freezing semantic polygon IDs and
        # loop UVs. Same-winding overlays keep the last authored material
        # (e.g. a wooden window reveal over the shell's default wall slot).
        groups={}
        for index,face in enumerate(self.faces):
            groups.setdefault(tuple(sorted(face)),[]).append(index)
        def oriented_key(face):
            return min(tuple(face[i:]+face[:i]) for i in range(len(face)))
        keep=set()
        for indices in groups.values():
            windings={oriented_key(self.faces[i]) for i in indices}
            if len(windings)==1:
                keep.add(indices[-1])
            elif len(windings)!=2:
                raise ValueError("E_KIT_FACE_OVERLAP: ambiguous coincident polygons")
            else:
                first=self.faces[indices[0]]
                if oriented_key(first[::-1]) not in windings:
                    raise ValueError("E_KIT_FACE_OVERLAP: incompatible polygon winding")
                # Opposite outward faces separate two joined closed pieces;
                # neither belongs to the exterior surface.
        selected=sorted(keep)
        self.faces=[self.faces[i] for i in selected]
        self.labels=[self.labels[i] for i in selected]
        self.uvs=[self.uvs[i] for i in selected]
        used=sorted({index for face in self.faces for index in face})
        remap={old:new for new,old in enumerate(used)}
        self.vertices=[self.vertices[i] for i in used]
        self.faces=[[remap[i] for i in face] for face in self.faces]
        mesh = bpy.data.meshes.new(object_id + "_mesh")
        mesh.from_pydata(self.vertices, [], self.faces)
        mesh.update()
        # Every primitive supplies outward winding. Recalculating disconnected
        # caps would choose arbitrary signs, and assembled wall patches may
        # legitimately share non-manifold internal edges.
        root=bpy.data.objects.new(object_id, None)
        obj=bpy.data.objects.new(object_id+"_geometry", mesh)
        bpy.context.collection.objects.link(root)
        bpy.context.collection.objects.link(obj)
        obj.parent=root
        regions={name: [] for name in spec["regions"]}
        uv_template={}
        for poly, label, indices, values in zip(mesh.polygons, self.labels, self.faces, self.uvs):
            regions[label].append(poly.index)
            table=dict(zip(indices, values))
            for li in poly.loop_indices:
                uv_template[li]=tuple(table[mesh.loops[li].vertex_index])
        mesh.uv_layers.new(name="UVMap")
        return KitInstance(object_id, kit_id, root, obj, regions, uv_template,
                           meta={"topology_sha256": topology_signature(mesh), "generator": "procedural_v1"})


def _house(b):
    # Front wall with a true arched opening, 0.75 x 1.75 m.
    b.box((-1,-1,0),(-.375,-.88,2),"wall_front")
    b.box((.375,-1,0),(1,-.88,2),"wall_front")
    xs=[-.375*math.cos(i*math.pi/8) for i in range(9)]
    zs=[1.375+.375*math.sin(i*math.pi/8) for i in range(9)]
    for i in range(8):
        b.extrude_xz([(xs[i],zs[i]),(xs[i+1],zs[i+1]),(xs[i+1],2),(xs[i],2)],-1,-.88,"wall_front","reveals")
    b.box((-1,.88,0),(1,1,2),"wall_back")
    for x0,x1,region in [(-1,-.88,"wall_left"),(.88,1,"wall_right")]:
        b.box((x0,-.88,0),(x1,-.25,2),region)
        b.box((x0,.25,0),(x1,.88,2),region)
        b.box((x0,-.25,0),(x1,.25,1),region)
        b.box((x0,-.25,1.5),(x1,.25,2),region)
        b.quad([(x0,-.25,1),(x1,-.25,1),(x1,.25,1),(x0,.25,1)],"reveals")
    b.box((-1,-1,-.10),(1,1,0),"floor")


def _roof(b):
    width=1.15; rise=width*math.tan(math.radians(35)); thick=.09
    b.quad([(-width,-1.15,thick),(0,-1.15,rise+thick),(0,1.15,rise+thick),(-width,1.15,thick)],"roof_left")
    b.quad([(0,-1.15,rise+thick),(width,-1.15,thick),(width,1.15,thick),(0,1.15,rise+thick)],"roof_right")
    for y,region in [(-1.15,"gable_front"),(1.15,"gable_back")]:
        pts=[(-width,y,0),(width,y,0),(0,y,rise)]
        uvs=[(0,1),(1,1),(.5,0)]
        b.face(pts if y<0 else pts[::-1],region,uvs if y<0 else uvs[::-1])
        for xa,za,xb,zb in [(-width,0,0,rise),(0,rise,width,0)]:
            pts=[(xa,y,za),(xb,y,zb),(xb,y,zb+thick),(xa,y,za+thick)]
            b.quad(pts if y<0 else pts[::-1],"trim")
    for x in [-width,width]:
        pts=[(x,-1.15,0),(x,1.15,0),(x,1.15,thick),(x,-1.15,thick)]
        b.quad(pts if x>0 else pts[::-1],"trim")
    b.quad([(-width,-1.15,0),(-width,1.15,0),(width,1.15,0),(width,-1.15,0)],"trim")


def _door(b):
    outer=[(-.375,0),(.375,0)]+[(.375*math.cos(i*math.pi/8),1.375+.375*math.sin(i*math.pi/8)) for i in range(9)]
    inner=[(-.30,0),(.30,0)]+[(.30*math.cos(i*math.pi/8),1.375+.30*math.sin(i*math.pi/8)) for i in range(9)]
    b.extrude_xz(inner,-.035,.05,"panel")
    for i in range(1,len(outer)):
        j=(i+1)%len(outer)
        b.extrude_xz([outer[i],outer[j],inner[j],inner[i]],-.07,.075,"frame")
    b.box((.18,-.12,.75),(.235,-.06,.82),"handle")


def _window(b):
    b.box((-.25,-.055,-.25),(-.19,.06,.25),"frame")
    b.box((.19,-.055,-.25),(.25,.06,.25),"frame")
    b.box((-.19,-.055,-.25),(.19,.06,-.19),"frame")
    b.box((-.19,-.055,.19),(.19,.06,.25),"frame")
    b.box((-.19,-.025,-.19),(.19,.015,.19),"glass")
    b.box((-.018,-.06,-.19),(.018,-.025,.19),"frame")
    b.box((-.19,-.06,-.018),(.19,-.025,.018),"frame")


def _beam(b):
    cross=[(-.035,-.05),(.035,-.05),(.05,-.035),(.05,.035),(.035,.05),(-.035,.05),(-.05,.035),(-.05,-.035)]
    for i in range(8):
        x,y=cross[i]; X,Y=cross[(i+1)%8]
        b.quad([(x,y,0),(X,Y,0),(X,Y,1),(x,y,1)],"body")
    for z in [0,1]:
        pts=[(x,y,z) for x,y in cross]
        b.face(pts if z>0 else pts[::-1],"endcap")


def _ramp(b):
    h=math.tan(math.radians(30))
    b.quad([(-.5,-.5,0),(.5,-.5,0),(.5,.5,h),(-.5,.5,h)],"slope")
    b.quad([(-.5,-.5,0),(-.5,.5,0),(.5,.5,0),(.5,-.5,0)],"body")
    b.quad([(-.5,.5,0),(-.5,.5,h),(.5,.5,h),(.5,.5,0)],"body")
    for x in [-.5,.5]:
        pts=[(x,-.5,0),(x,.5,0),(x,.5,h)]
        b.face(pts if x>0 else pts[::-1],"body")


def _barrel(b):
    rings=[(0,.22),(.07,.235),(.11,.245),(.30,.265),(.49,.245),(.53,.235),(.60,.22)]
    for k,((z,r),(Z,R)) in enumerate(zip(rings,rings[1:])):
        for i in range(12):
            a=i*math.tau/12; c=(i+1)*math.tau/12
            b.quad([(r*math.cos(a),r*math.sin(a),z),(r*math.cos(c),r*math.sin(c),z),(R*math.cos(c),R*math.sin(c),Z),(R*math.cos(a),R*math.sin(a),Z)],"bands" if k in [1,4] else "body",grid=False)
    for z,r in [rings[0],rings[-1]]:
        pts=[(r*math.cos(i*math.tau/12),r*math.sin(i*math.tau/12),z) for i in range(12)]
        b.face(pts if z>0 else pts[::-1],"endcap")


def humanoid_bones():
    """Fixed A-pose rest coordinates. Bone local Y is its head-to-tail axis."""
    bones={
        "root": ((0,0,0),(0,0,.12),None),
        "pelvis": ((0,0,.64),(0,0,.79),"root"),
        "spine": ((0,0,.79),(0,0,.96),"pelvis"),
        "chest": ((0,0,.96),(0,0,1.10),"spine"),
        "neck": ((0,0,1.10),(0,0,1.25),"chest"),
        "head": ((0,0,1.25),(0,0,1.54),"neck"),
    }
    for side,sgn in [("l",1),("r",-1)]:
        sh=(sgn*.25,0,1.07); elbow=(sgn*.425,0,.88); wrist=(sgn*.555,0,.71)
        hip=(sgn*.125,0,.69); knee=(sgn*.14,0,.40); ankle=(sgn*.14,0,.12)
        bones.update({
            "clavicle_"+side: ((0,0,1.07),sh,"chest"),
            "upper_arm_"+side: (sh,elbow,"clavicle_"+side),
            "forearm_"+side: (elbow,wrist,"upper_arm_"+side),
            "hand_"+side: (wrist,(sgn*.615,0,.63),"forearm_"+side),
            "thigh_"+side: (hip,knee,"pelvis"),
            "shin_"+side: (knee,ankle,"thigh_"+side),
            "foot_"+side: (ankle,(sgn*.14,-.14,.08),"shin_"+side),
        })
    return bones


def _segment_distance(p,a,b):
    from mathutils import Vector
    a,b=Vector(a),Vector(b); delta=b-a
    t=max(0.,min(1.,(p-a).dot(delta)/delta.length_squared))
    return (p-(a+t*delta)).length, t


def _region_for(co, bones):
    x,y,z=co
    if z>1.205:
        return "face" if y<-.07 and abs(x)<.19 and 1.24<z<1.52 else "head"
    if z>1.105 and abs(x)<.16: return "neck"
    side="l" if x>=0 else "r"
    if z<.70 and abs(x)<.32:
        if z<.16: return "foot_"+side
        if z<.40: return "shin_"+side
        if z<.63: return "thigh_"+side
        return "pelvis"
    if abs(x)>.235:
        candidates=["upper_arm_"+side,"forearm_"+side,"hand_"+side]
        return min(candidates,key=lambda n:_segment_distance(co,*bones[n][:2])[0])
    return "pelvis" if z<.77 else "torso"


def _humanoid(object_id,kit_id,spec):
    import bpy
    from mathutils import Vector
    # One branching edge skeleton -> one welded manifold Skin surface.
    # Applying the modifier bakes the deterministic topology before assigning
    # region charts and vertex-ID weights. There are no intersecting capsules.
    verts=[(0,0,.70),(0,0,.84),(0,0,1.04),(0,0,1.17),(0,0,1.34),(0,0,1.47)]
    radii=[(.18,.12),(.19,.125),(.21,.13),(.075,.075),(.22,.20),(.16,.16)]
    edges=[(i,i+1) for i in range(5)]
    for sgn in [1,-1]:
        chain=[(sgn*.25,0,1.07),(sgn*.34,0,.975),(sgn*.425,0,.88),(sgn*.49,0,.795),(sgn*.555,0,.71),(sgn*.615,0,.63)]
        prev=2
        for co,r in zip(chain,[.105,.085,.075,.067,.06,.065]):
            idx=len(verts);verts.append(co);radii.append((r,r));edges.append((prev,idx));prev=idx
        chain=[(sgn*.125,0,.66),(sgn*.14,0,.53),(sgn*.14,0,.40),(sgn*.14,0,.26),(sgn*.14,0,.12),(sgn*.14,-.14,.08)]
        prev=0
        for co,r in zip(chain,[.12,.105,.09,.085,.075,.08]):
            idx=len(verts);verts.append(co);radii.append((r,r));edges.append((prev,idx));prev=idx
    mesh=bpy.data.meshes.new(object_id+"_cage");mesh.from_pydata(verts,edges,[]);mesh.update()
    obj=bpy.data.objects.new(object_id+"_geometry",mesh);bpy.context.collection.objects.link(obj)
    bpy.ops.object.select_all(action="DESELECT");obj.select_set(True);bpy.context.view_layer.objects.active=obj
    skin=obj.modifiers.new("Trusted connected skin cage","SKIN")
    for data,r in zip(mesh.skin_vertices[0].data,radii): data.radius=r
    mesh.skin_vertices[0].data[0].use_root=True
    skin.branch_smoothing=.5
    bpy.ops.object.modifier_apply(modifier=skin.name)
    sub=obj.modifiers.new("Fixed low poly rounding","SUBSURF");sub.levels=1;sub.render_levels=1
    bpy.ops.object.modifier_apply(modifier=sub.name)
    # Skin retains isolated cage branch points in some Blender builds. Strip
    # vertices not referenced by faces before freezing the published topology.
    baked=obj.data
    used=sorted({index for polygon in baked.polygons for index in polygon.vertices})
    if len(used)!=len(baked.vertices):
        remap={old:new for new,old in enumerate(used)}
        cleaned=bpy.data.meshes.new(object_id+"_mesh")
        cleaned.from_pydata([baked.vertices[i].co[:] for i in used],[],
            [[remap[i] for i in polygon.vertices] for polygon in baked.polygons])
        cleaned.update();obj.data=cleaned;bpy.data.meshes.remove(baked)
    root=bpy.data.objects.new(object_id,None);bpy.context.collection.objects.link(root);obj.parent=root
    mesh=obj.data;mesh.update();bones=humanoid_bones()
    regions={name:[] for name in spec["regions"]};uv={}
    for poly in mesh.polygons:
        co=poly.center
        region=_region_for(co,bones);regions[region].append(poly.index)
        poly.use_smooth=False
    # Shared normalized chart for every semantic surface, cylindrical about
    # its limb axis. Explicit angular seam only; no per-triangle restamping.
    for region,indices in regions.items():
        if not indices: continue
        vids={v for i in indices for v in mesh.polygons[i].vertices}
        coords=[mesh.vertices[i].co for i in vids]
        if region=="face":
            xmin,xmax=min(p.x for p in coords),max(p.x for p in coords)
            zmin,zmax=min(p.z for p in coords),max(p.z for p in coords)
            for i in indices:
                for li in mesh.polygons[i].loop_indices:
                    p=mesh.vertices[mesh.loops[li].vertex_index].co
                    uv[li]=((p.x-xmin)/max(xmax-xmin,1e-6),1-(p.z-zmin)/max(zmax-zmin,1e-6))
            continue
        name="spine" if region=="torso" else region
        if name not in bones: name="head"
        a,b,_=bones[name];a=Vector(a);axis=(Vector(b)-a).normalized()
        basis=Vector((0,-1,0));basis=(basis-axis*basis.dot(axis)).normalized()
        other=axis.cross(basis).normalized()
        along=[(p-a).dot(axis) for p in coords];low,high=min(along),max(along)
        chart_values={}
        for i in indices:
            loops=list(mesh.polygons[i].loop_indices);values=[]
            for li in loops:
                p=mesh.vertices[mesh.loops[li].vertex_index].co-a
                # Seam at the rear (+Y) of each limb.
                s=(math.atan2(p.dot(other),p.dot(basis))/math.tau+.5)%1
                t=1-(p.dot(axis)-low)/max(high-low,1e-6)
                values.append((s,t))
            if max(s for s,t in values)-min(s for s,t in values)>.5:
                values=[(s+1 if s<.5 else s,t) for s,t in values]
            for li,value in zip(loops,values): chart_values[li]=value
        smax=max(s for s,t in chart_values.values())
        smin=min(s for s,t in chart_values.values())
        for li,(s,t) in chart_values.items():
            uv[li]=((s-smin)/max(smax-smin,1e-6),max(0.,min(1.,t)))
    # Regions sharing a texture occupy separate authored islands. Upper-arm
    # rectangles are reserved in both shirt and skin so assign_slot does not
    # change the chart or cause overlaps. Gaps allow internal edge dilation.
    charts={
        "face":(0,0,1,1), "head":(.025,.035,.28,.93),
        "neck":(.34,.035,.12,.38),
        "upper_arm_l":(.495,.035,.22,.38),
        "upper_arm_r":(.75,.035,.225,.38),
        "forearm_l":(.34,.50,.20,.465),
        "forearm_r":(.575,.50,.20,.465),
        "hand_l":(.81,.50,.165,.20), "hand_r":(.81,.765,.165,.20),
        "torso":(.025,.035,.435,.93),
        "pelvis":(.025,.025,.95,.18),
        "thigh_l":(.025,.24,.445,.335),"thigh_r":(.53,.24,.445,.335),
        "shin_l":(.025,.61,.445,.365),"shin_r":(.53,.61,.445,.365),
        "foot_l":(.025,.035,.445,.93),"foot_r":(.53,.035,.445,.93),
    }
    for region,indices in regions.items():
        x,y,w,h=charts[region]
        for pi in indices:
            for li in mesh.polygons[pi].loop_indices:
                s,t=uv[li];uv[li]=(x+w*s,y+h*t)
    mesh.uv_layers.new(name="UVMap")
    return KitInstance(object_id,kit_id,root,obj,regions,uv,
        meta={"topology_sha256":topology_signature(mesh),"topology_revision":"humanoid_round_topology_v1",
              "generator":"skin_cage_subdiv1_v1","bone_specs":bones,
              "uv_charts":"continuous cylindrical regions with explicit rear seam; planar face",
              "region_chart_rects":charts})


def build_kit(kit_id: str, object_id: str, catalog: dict) -> KitInstance:
    spec=next((k for k in catalog["kits"] if k["id"]==kit_id),None)
    if spec is None: raise ValueError("E_UNKNOWN_KIT: "+kit_id)
    if kit_id=="humanoid_round_v1": return _humanoid(object_id,kit_id,spec)
    generators={"house_shell_2m_v1":_house,"roof_gable_35_v1":_roof,
                "door_roundtop_v1":_door,"window_square_v1":_window,
                "beam_bevel_v1":_beam,"ramp_30_v1":_ramp,"barrel_12_v1":_barrel}
    if kit_id not in generators: raise ValueError("E_UNIMPLEMENTED_KIT: "+kit_id)
    b=_Builder();generators[kit_id](b)
    return b.finish(object_id,kit_id,spec)

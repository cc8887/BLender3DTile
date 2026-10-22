"""Backend-independent contract rejection and semantic planning regressions."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from b3dtile.contracts import (ContractError, canonical_bytes, catalog_digest,
                               load_catalog, load_schema, parse_json,
                               validate_plan, validate_request)


ROOT=Path(__file__).parents[1]


def example(name="house"):
    request=json.loads((ROOT/"examples"/(name+".request.json")).read_text())
    request["params"]["catalog_sha256"]=catalog_digest()
    return request


def minimal(kind="prop"):
    request=example("zombie" if kind=="character" else "house")
    place=next(op for op in request["params"]["ops"] if op["op"]=="place")
    request["params"]["ops"]=[place,{"op":"export_glb","profile":"game_pixel_v1","clips":"none"}]
    return request


def rejection(request,code):
    with pytest.raises(ContractError) as error:
        validate_request(request)
    assert error.value.diagnostic["code"]==code,error.value.diagnostic
    return error.value.diagnostic


def test_schema_and_both_supplied_examples_are_valid():
    Draft202012Validator.check_schema(load_schema())
    assert set(validate_request(example())["objects"])=={"walls","roof","door","window_l","window_r"}
    assert validate_request(example("zombie"))["bound"]==["zombie"]


@pytest.mark.parametrize("field,value",[
    ("kit_id","arbitrary_mesh"),("rot","pitch_35"),("scale",-1),
    ("scale",1.25),("pos",[.1,0,0]),("pos",[0,0,0,0]),
])
def test_non_enumerated_kit_transform_or_offgrid_position_is_rejected(field,value):
    request=minimal();request["params"]["ops"][0][field]=value
    rejection(request,"E_SCHEMA")


def test_scripts_extra_fields_and_unknown_operations_are_rejected():
    for mutation in [lambda r:r["params"]["ops"][0].update(script="import bpy"),
                     lambda r:r["params"]["ops"].insert(1,{"op":"python","code":"print(1)"}),
                     lambda r:r["params"].update(output_path="C:/arbitrary.glb")]:
        request=minimal();mutation(request);rejection(request,"E_SCHEMA")


@pytest.mark.parametrize("source",['{"x":NaN}','{"x":Infinity}','{"x":-Infinity}'])
def test_strict_json_rejects_nonfinite_constants(source):
    with pytest.raises(ContractError) as error:parse_json(source)
    assert error.value.diagnostic["code"]=="E_JSON_NONFINITE"


@pytest.mark.parametrize("source",['{"x":1,"x":2}','{"outer":{"x":1,"x":2}}'])
def test_strict_json_rejects_duplicate_keys_at_every_depth(source):
    with pytest.raises(ContractError) as error:parse_json(source)
    assert error.value.diagnostic["code"]=="E_JSON_DUPLICATE_KEY"


def test_python_nan_and_overflowed_json_cannot_pass_the_request_boundary():
    for value in [float("nan"),float("inf"),parse_json('{"number":1e400}')["number"]]:
        request=minimal();request["params"]["ops"][0]["pos"][0]=value
        rejection(request,"E_JSON")
    with pytest.raises(ContractError):canonical_bytes({"number":float("nan")})


@pytest.mark.parametrize("operation,code",[
    ({"op":"assign_slot","mesh":"walls","region":"torso","slot":"shirt"},"E_REGION_UNKNOWN"),
    ({"op":"assign_slot","mesh":"walls","region":"wall_front","slot":"shirt"},"E_SLOT_REGION"),
    ({"op":"set_region_uv","mesh":"walls","region":"wall_front","tile_id":"env_roof_rust","orientation":0},"E_REGION_TILE_MISMATCH"),
    ({"op":"assign_slot","mesh":"missing","region":"wall_front","slot":"wall"},"E_OBJECT_UNKNOWN"),
])
def test_object_region_slot_and_tile_cross_references_are_checked(operation,code):
    request=minimal();request["params"]["ops"].insert(1,operation)
    diagnostic=rejection(request,code)
    assert diagnostic["op_index"]==1


def test_assigning_arm_to_skin_then_mapping_skin_is_supported():
    request=minimal("character")
    request["params"]["ops"][1:1]=[
        {"op":"assign_slot","mesh":"zombie","region":"upper_arm_l","slot":"skin"},
        {"op":"set_region_uv","mesh":"zombie","region":"upper_arm_l","tile_id":"char_skin_mint","orientation":0},
        {"op":"bind_humanoid","mesh":"zombie","preset":"humanoid_20_v1"},
    ]
    assert validate_request(request)["bound"]==["zombie"]


def test_character_chart_cannot_be_freely_rotated():
    request=example("zombie")
    op=next(op for op in request["params"]["ops"] if op["op"]=="set_region_uv")
    op["orientation"]=90
    rejection(request,"E_UV_ORIENTATION")


def test_socket_name_must_exist_on_its_specific_kit():
    request=example()
    attach=next(op for op in request["params"]["ops"] if op["op"]=="attach")
    attach["child_socket"]="mount"  # Valid enum, but not a roof socket.
    rejection(request,"E_SOCKET_UNKNOWN")


def test_socket_scale_mismatch_and_occupied_socket_are_rejected():
    request=example()
    next(op for op in request["params"]["ops"] if op.get("id")=="roof")["scale"]=2
    rejection(request,"E_SOCKET_FIT")
    request=example()
    request["params"]["ops"][-1:-1]=[
        {"op":"place","id":"second_roof","kit_id":"roof_gable_35_v1","pos":[0,0,0],"rot":"yaw_0","scale":1},
        {"op":"attach","child":"second_roof","child_socket":"base","parent":"walls","parent_socket":"roof_mount","mate":"flush"},
    ]
    rejection(request,"E_SOCKET_OCCUPIED")


def test_cycle_guard_remains_effective_when_a_future_catalog_allows_matching_ports():
    # Production socket types are acyclic already. A trusted synthetic catalog
    # makes the general graph invariant reachable without invalidating ports.
    catalog=deepcopy(load_catalog())
    house=next(k for k in catalog["kits"] if k["id"]=="house_shell_2m_v1")
    house["sockets"]["base"]=deepcopy(house["sockets"]["roof_mount"])
    catalog["socket_types"]["roof_2m"]["certified_pairs"].append(
        ["house_shell_2m_v1:base","house_shell_2m_v1:roof_mount"])
    request=minimal();a=request["params"]["ops"][0];a["id"]="a"
    b=deepcopy(a);b["id"]="b"
    request["params"]["ops"][1:1]=[b,
        {"op":"attach","child":"b","child_socket":"base","parent":"a","parent_socket":"roof_mount","mate":"flush"},
        {"op":"attach","child":"a","child_socket":"base","parent":"b","parent_socket":"roof_mount","mate":"flush"}]
    with pytest.raises(ContractError) as error:validate_plan(request["params"],catalog)
    assert error.value.diagnostic["code"]=="E_ATTACH_CYCLE"


def test_unbound_character_and_pose_before_binding_are_rejected():
    rejection(minimal("character"),"E_RIG_MISSING")
    request=example("zombie");ops=request["params"]["ops"]
    index=next(i for i,op in enumerate(ops) if op["op"]=="bind_humanoid")
    ops.insert(index,{"op":"render_preview","camera":"front","pose":"arms_raise_90","texture_view":"final"})
    rejection(request,"E_POSE_UNBOUND")


def test_export_must_be_last_and_exist_exactly_once():
    request=minimal();request["params"]["ops"].append({"op":"validate","profile":"prop_v1"})
    rejection(request,"E_EXPORT_ORDER")
    request=minimal();request["params"]["ops"].append(deepcopy(request["params"]["ops"][-1]))
    rejection(request,"E_SCHEMA")
    request=minimal();request["params"]["ops"].pop()
    rejection(request,"E_SCHEMA")


def test_public_observation_schema_requires_explicit_revision_for_image_reads():
    request={"method":"read_preview","request_id":"read_one","params":{
        "asset_id":"house_demo","revision":"latest","artifact_id":"a_0123456789ab"}}
    rejection(request,"E_SCHEMA")
    request["params"]["revision"]="0123456789abcdef"
    assert validate_request(request)["objects"]=={}


def test_contract_loading_and_validation_do_not_import_blender():
    code=("import sys; from b3dtile.contracts import load_catalog, load_schema; "
          "load_catalog(); load_schema(); assert 'bpy' not in sys.modules")
    result=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr

"""Real stdio protocol tests; no Blender build is needed for transport checks."""
import asyncio
import base64
from datetime import timedelta
import json
import os
from pathlib import Path
import struct
import sys

import pytest
from PIL import Image
from jsonschema import Draft202012Validator

pytest.importorskip("mcp")
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent,TextContent

from b3dtile.contracts import catalog_digest,file_sha256
from b3dtile.service import AssetService,write_json


ROOT=Path(__file__).parents[1]


async def with_session(output_root,check):
    env={**os.environ,"PYTHONUTF8":"1","PYTHONPATH":str(ROOT/"src")}
    params=StdioServerParameters(command=sys.executable,
        args=["-m","b3dtile.mcp_server","--output-root",str(output_root)],env=env,cwd=str(ROOT))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=15)) as session:
            initialized=await session.initialize()
            assert initialized.serverInfo.name=="blender3dtile"
            return await check(session)


def text_result(result):
    return json.loads(next(item.text for item in result.content if isinstance(item,TextContent)))


def test_real_stdio_lists_enumerated_tools_and_returns_structured_errors(tmp_path):
    async def check(session):
        listed=await session.list_tools()
        tools={tool.name:tool for tool in listed.tools}
        assert set(tools)=={"get_catalog","build_asset","observe_asset","inspect_object","read_preview"}
        build_schema=tools["build_asset"].inputSchema
        Draft202012Validator.check_schema(build_schema)
        assert build_schema["properties"]["recipe"]["$ref"]=="#/$defs/recipe"
        assert "humanoid_round_v1" in build_schema["$defs"]["kit_id"]["enum"]
        assert len(build_schema["$defs"]["kit_id"]["enum"])==8
        assert not tools["build_asset"].annotations.readOnlyHint
        assert tools["observe_asset"].annotations.readOnlyHint
        catalog=await session.call_tool("get_catalog",{})
        assert not catalog.isError
        data=text_result(catalog)
        assert data["catalog_sha256"]==catalog_digest()
        assert data["catalog"]["catalog_version"]=="starter_v1"
        assert set(build_schema["$defs"]["kit_id"]["enum"])=={k["id"] for k in data["catalog"]["kits"]}
        missing=await session.call_tool("observe_asset",{"asset_id":"never_built"})
        assert missing.isError
        assert text_result(missing)=={"ok":False,"error":{
            "code":"E_NO_SUCCESSFUL_BUILD","message":"No successfully published model exists","asset_id":"never_built"}}
        invalid={"asset_id":"never_built","revision":"latest","artifact_id":"a_0123456789ab"}
        assert list(Draft202012Validator(tools["read_preview"].inputSchema).iter_errors(invalid))
        rejected=await session.call_tool("read_preview",invalid)
        assert rejected.isError
        # The real SDK rejects these arguments before entering the tool body.
        assert "Input validation error" in " ".join(x.text for x in rejected.content if isinstance(x,TextContent))
    asyncio.run(asyncio.wait_for(with_session(tmp_path,check),timeout=35))


def publish_small_fixture(output_root):
    """Use the real immutable-packet helpers with a tiny valid GLB and PNG."""
    service=AssetService(output_root);asset_id="mcp_fixture";revision="0123456789abcdef"
    asset_dir=service.root/asset_id;directory=asset_dir/"revisions"/revision
    directory.mkdir(parents=True)
    image_path=directory/"contact_sheet.png"
    Image.new("RGB",(6,4),(80,150,110)).save(image_path)
    chunk=json.dumps({"asset":{"version":"2.0"},"scene":0,"scenes":[{}]},separators=(",",":")).encode()
    chunk+=b" "*((-len(chunk))%4)
    (directory/(asset_id+".glb")).write_bytes(struct.pack("<III",0x46546C67,2,20+len(chunk))+
        struct.pack("<II",len(chunk),0x4E4F534A)+chunk)
    write_json(directory/"observation.json",{
        "observation_version":"1.0","asset_id":asset_id,"revision":revision,
        "glb":asset_id+".glb","contact_sheet":"contact_sheet.png",
        "objects":[{"id":"walls","kit_id":"house_shell_2m_v1","vertices":8,"triangles":12,
                    "regions":{"wall_front":{"slot":"wall"}}}],
        "images":[{"path":"contact_sheet.png","revision":revision,"sha256":file_sha256(image_path),
                   "dimensions":[6,4],"camera":"front","pose":"rest","mode":"contact_sheet"}],
        "diagnostics":{"geometry":{"ok":True},"glb":{"ok":True}},
    })
    manifest={"asset_id":asset_id,"revision":revision,"build_sha256":"1"*64,
              "artifacts":service._collect_artifacts(directory),"validation":{"ok":True}}
    write_json(directory/"manifest.json",manifest)
    service._verify_packet(directory,manifest)
    service._publish(asset_dir,directory,manifest,"fixture_request",asset_dir/"requests"/"fixture_request.json","2"*64,cached=False)
    artifact_id=next(aid for aid,a in manifest["artifacts"].items() if a["path"]=="contact_sheet.png")
    return asset_id,revision,artifact_id,image_path.read_bytes()


def test_real_stdio_observation_resolves_revision_and_preview_is_native_image(tmp_path):
    asset_id,revision,artifact_id,png=publish_small_fixture(tmp_path)
    async def check(session):
        observed=await session.call_tool("observe_asset",{"asset_id":asset_id})
        assert not observed.isError
        data=text_result(observed)
        assert data["revision"]==revision
        assert data["freshness"]["resolved_revision"]==revision
        assert data["freshness"]["latest_attempt_status"]=="succeeded"
        assert data["images"][0]["artifact_id"]==artifact_id
        inspected=await session.call_tool("inspect_object",{"asset_id":asset_id,"revision":revision,"object_id":"walls"})
        assert not inspected.isError
        assert text_result(inspected)["object"]["regions"]["wall_front"]["slot"]=="wall"
        preview=await session.call_tool("read_preview",{"asset_id":asset_id,"revision":revision,"artifact_id":artifact_id})
        assert not preview.isError
        assert len(preview.content)==1 and isinstance(preview.content[0],ImageContent)
        assert preview.content[0].mimeType=="image/png"
        assert base64.b64decode(preview.content[0].data)==png
    asyncio.run(asyncio.wait_for(with_session(tmp_path,check),timeout=35))

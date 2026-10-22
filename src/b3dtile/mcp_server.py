"""Thin stdio MCP adapter. Construction inputs retain the full enum schema."""
import argparse
import asyncio
import base64
import json
from pathlib import Path

from .contracts import ContractError, load_catalog, load_schema
from .service import AssetService


def make_server(output_root):
    from mcp.server import Server
    from mcp.types import Tool, ToolAnnotations, TextContent, ImageContent, CallToolResult
    service = AssetService(output_root)
    server = Server("blender3dtile", version="0.1.0", instructions=(
        "Read get_catalog before building. Only submit enumerated recipes. After a build, use observe_asset; "
        "read the contact sheet with read_preview using the explicit resolved revision and artifact_id. "
        "Use inspect_object for a problem region. Send expected_revision and a new request_id for edits. "
        "Never interpret a previous successful model as the result of a failed latest attempt."))

    @server.list_tools()
    async def list_tools():
        schema = load_schema()
        defs = schema["$defs"]
        result = []
        descriptions = {
            "get_catalog": "Get supported kits, atlas/rig enums, compatibility rules and the current recipe schema/hash.",
            "build_asset": "Build a complete asset in an isolated bpy process. Atomic publication includes GLB, previews and validation. May take tens of seconds.",
            "observe_asset": "Resolve latest once. Return a compact revision-bound model summary, image artifact IDs, freshness and optional changes since known_revision.",
            "inspect_object": "Inspect one stable object ID and its regions/slots/bounds in an explicit model revision.",
            "read_preview": "Return a native PNG image from the exact observed revision. Start with the contact_sheet artifact; arbitrary paths are not accepted.",
        }
        for name, description in descriptions.items():
            if name == "get_catalog":
                inputs = {"type": "object", "properties": {}, "additionalProperties": False}
            elif name == "build_asset":
                inputs = {"type": "object", "additionalProperties": False, "properties": {
                    "recipe": {"$ref": "#/$defs/recipe"}, "request_id": {"$ref": "#/$defs/id"},
                    "expected_revision": defs["build_asset"]["properties"]["expected_revision"]}, "required": ["recipe", "request_id"], "$defs": defs}
            else:
                inputs = {**defs[name]["properties"]["params"], "$defs": defs}
            result.append(Tool(name=name, description=description, inputSchema=inputs,
                annotations=ToolAnnotations(readOnlyHint=name != "build_asset", destructiveHint=False,
                                            idempotentHint=True, openWorldHint=False)))
        return result

    @server.call_tool()
    async def call_tool(name, arguments):
        args = arguments or {}
        try:
            if name == "get_catalog":
                result = service.get_catalog()
            elif name == "build_asset":
                request = {"method": name, "request_id": args["request_id"], "params": args["recipe"]}
                if "expected_revision" in args:
                    request["expected_revision"] = args["expected_revision"]
                result = await asyncio.to_thread(service.execute, request)
            elif name == "observe_asset":
                result = await asyncio.to_thread(service.observe_asset, **args)
            elif name == "inspect_object":
                result = await asyncio.to_thread(service.inspect_object, **args)
            elif name == "read_preview":
                path = service.artifact_path(**args, images_only=True)
                return [ImageContent(type="image", mimeType="image/png", data=base64.b64encode(path.read_bytes()).decode("ascii"))]
            else:
                raise ContractError("E_METHOD", "Unknown tool")
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                  isError=result.get("ok") is False)
        except ContractError as exc:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": exc.diagnostic}))], isError=True)
    return server


def main():
    from mcp.server.stdio import stdio_server
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=str(Path.cwd() / "out"))
    args = parser.parse_args()
    server = make_server(args.output_root)
    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main()

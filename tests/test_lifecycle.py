"""Exercise native Windows bpy shutdown in real rendered subprocesses."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

from PIL import Image
import pytest


pytestmark=[
    pytest.mark.blender,
    pytest.mark.skipif(sys.platform!="win32",reason="Windows native worker termination regression"),
    pytest.mark.skipif(importlib.util.find_spec("bpy") is None,reason="pinned bpy backend is not installed"),
]


@pytest.mark.parametrize("exit_code",[0,1])
def test_finish_process_preserves_status_after_actual_cycles_render_and_save(tmp_path,exit_code):
    # os._exit() used to return STATUS_ACCESS_VIOLATION after this workload,
    # despite worker_result being completely written. Mocking native exit
    # APIs cannot expose that DLL teardown failure.
    script=textwrap.dedent("""
        import json
        import sys
        from pathlib import Path
        import bpy
        from b3dtile.lifecycle import finish_process

        output=Path(sys.argv[1])
        status=int(sys.argv[2])
        bpy.ops.wm.read_factory_settings(use_empty=False)
        scene=bpy.context.scene
        scene.render.engine="CYCLES"
        scene.cycles.device="CPU"
        scene.cycles.samples=1
        scene.render.threads_mode="FIXED"
        scene.render.threads=2
        scene.render.resolution_x=32
        scene.render.resolution_y=32
        scene.render.resolution_percentage=100
        scene.render.filepath=str(output/"tiny.png")
        bpy.ops.render.render(write_still=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(output/"debug.blend"))
        (output/"worker_result.json").write_text(json.dumps({
            "render_completed":True,"requested_status":status,
            "bpy_loaded":"bpy" in sys.modules,
        }),encoding="utf-8")
        finish_process(status)
        raise AssertionError("finish_process unexpectedly returned")
    """)
    repo=Path(__file__).parents[1]
    env={**os.environ,"PYTHONUTF8":"1","PYTHONPATH":str(repo/"src")}
    completed=subprocess.run([sys.executable,"-c",script,str(tmp_path),str(exit_code)],
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=60,env=env)
    assert completed.returncode==exit_code,(completed.returncode,completed.stdout[-3000:],completed.stderr[-3000:])
    assert json.loads((tmp_path/"worker_result.json").read_text())=={
        "render_completed":True,"requested_status":exit_code,"bpy_loaded":True}
    assert (tmp_path/"debug.blend").stat().st_size>0
    with Image.open(tmp_path/"tiny.png") as image:
        assert image.size==(32,32)
        image.verify()

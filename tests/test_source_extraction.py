"""Source intake preserves data; it never claims a new topology is certified."""
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


intake = load("source_intake", ROOT / "tools/extract_source_glb.py")
fixture = load("source_glb_fixture", ROOT / "tests/test_glb.py")


def test_source_skin_weights_and_animation_are_preserved(tmp_path):
    path = tmp_path / "source.glb"
    doc, binary = fixture.make_glb(path, skinned=True, animation=True)
    pack = intake.extract(path, "Crocotile-compatible GLB fixture", "test fixture")
    assert pack["certified"] is False
    assert len(pack["skins"][0]["joints"]) == 20
    assert pack["meshes"][0]["primitives"][0]["attributes"]["WEIGHTS_0"] == [(1., 0., 0., 0.)] * 4
    assert pack["animations"][0]["samplers"][0]["times"] == [(0.,), (1.,)]
    assert pack["source"]["label"] == "Crocotile-compatible GLB fixture"


def test_encoded_uv_is_flagged_and_preserved_not_clamped(tmp_path):
    path = tmp_path / "encoded.glb"
    doc, binary = fixture.make_glb(path)
    index = doc["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]
    binary = fixture.alter_accessor(doc, binary, index, [101., 200.] * 4)
    fixture._write_glb(path, doc, binary)
    pack = intake.extract(path)
    assert pack["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"][0] == (101., 200.)
    assert any("encoded UV animation" in note for note in pack["notes"])


def test_intake_rejects_external_buffers(tmp_path):
    path = tmp_path / "external.glb"
    doc, binary = fixture.make_glb(path)
    doc["buffers"][0]["uri"] = "external.bin"
    fixture._write_glb(path, doc, binary)
    with pytest.raises(ValueError, match="External buffers"):
        intake.extract(path)

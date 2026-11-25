from fractions import Fraction
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from video_engine.compiler import build_graph
from video_engine.media import Source
from video_engine.planner import plan
from video_engine.server import RequestError, span
from video_engine.timeline import EngineError, SCHEMA, layout, lengths, validate_spec
from video_engine.verify import top_level_boxes


def sources(seconds, root):
    return [Source(root / f"{i}.mp4", 0, None if i == 2 else 1, 1920, 1080, 90 if i == 1 else 0,
                   30 * n, Fraction(0), "bt709", "tv") for i, n in enumerate(seconds)]


def test_planner_opener_budget_and_replay(tmp_path):
    inputs = sources([7, 7, 1, 7, 7], tmp_path)
    edit = plan(inputs, root=tmp_path)
    assert edit == plan(inputs, root=tmp_path)
    assert [clip["path"] for clip in edit["clips"]] == [f"{i}.mp4" for i in [1, 0, 2, 3, 4]]
    assert layout(edit)[1] == 810
    assert lengths(edit)[2] == 30
    assert [t["type"] for t in edit["transitions"]] == ["dissolve", "cut", "cut", "dissolve"]
    Draft202012Validator.check_schema(SCHEMA)
    Draft202012Validator(SCHEMA).validate(edit)


def test_uses_cuts_to_reach_minimum(tmp_path):
    spec = plan(sources([5]*5, tmp_path), root=tmp_path)
    assert layout(spec)[1] == 750
    assert all(join["frames"] == 0 for join in spec["transitions"])
    with pytest.raises(EngineError, match="less than 25 seconds"):
        plan(sources([4]*5, tmp_path), root=tmp_path)
    with pytest.raises(EngineError, match="five distinct"):
        plan(sources([9]*4, tmp_path), root=tmp_path)


@pytest.mark.parametrize("change,error", [
    (lambda s: s.update(version=2), "version"),
    (lambda s: s["clips"][0].update(inFrame=1.0), "integer"),
    (lambda s: s["clips"][0].update(outFrame=2), "exclusive frame"),
    (lambda s: s["branding"].update(backgroundColor="bad"), "backgroundColor"),
    (lambda s: s["branding"]["textOverlays"][0].update(endFrame=900), "within the finished timeline"),
])
def test_invalid_timeline(tmp_path, change, error):
    spec = plan(sources([7]*5, tmp_path), root=tmp_path, branding={"text": "Hello"})
    change(spec)
    with pytest.raises(EngineError, match=error):
        validate_spec(spec)


def test_filter_plan_has_integer_join_clocks_and_silence(tmp_path):
    inputs = sources([7, 7, 1, 7, 7], tmp_path)
    spec = plan(inputs, root=tmp_path, branding={"text": "User's text: [x]; not a filter"})
    ordered = [next(src for src in inputs if src.path.name == clip["path"]) for clip in spec["clips"]]
    graph, audio = build_graph(spec, ordered, [(Path("title.png"), "72", "112", "")])
    assert graph.count("settb=1/30") == 7
    assert graph.count("xfade=transition=fade:duration=0.600000000") == 2
    assert graph.count("acrossfade=ns=28800") == 2
    assert "anullsrc=r=48000:cl=stereo,atrim=end_sample=48000" in graph
    assert "User's text" not in graph
    assert audio == "am4"
    spec["audio"] = "mute"
    graph, audio = build_graph(spec, ordered, [])
    assert audio is None and "atrim" not in graph


def test_byte_range_and_mp4_box_headers(tmp_path):
    assert span("bytes=0-1", 100) == (0, 1)
    assert span("bytes=-12", 100) == (88, 99)
    assert span("bytes=98-", 100) == (98, 99)
    with pytest.raises(RequestError, match="outside"):
        span("bytes=119-", 100)
    p = tmp_path / "out.mp4"
    p.write_bytes(b"\x00\x00\x00\x08moov\x00\x00\x00\x00mdatpayload")
    assert top_level_boxes(p) == [b"moov", b"mdat"]
    p.write_bytes(b"\x00\x00\x00\x01mdat")
    with pytest.raises(EngineError, match="Incomplete"):
        top_level_boxes(p)

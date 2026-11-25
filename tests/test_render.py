import hashlib
import json
import os
import statistics
import threading
from array import array
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from video_engine.branding import title_image
from video_engine.media import command, inspect_source, require_tools
from video_engine.planner import autoedit
from video_engine.renderer import render
from video_engine.server import CLIPS, LocalServer
from video_engine.timeline import EngineError, layout, lengths
from video_engine.verify import VIDEO_PROFILE

COLORS = ["red", "lime", "blue", "yellow", "magenta"]
RGB = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255)]


@pytest.fixture(scope="module")
def tools():
    found = os.getenv("VIDEO_ENGINE_FFMPEG", "ffmpeg"), os.getenv("VIDEO_ENGINE_FFPROBE", "ffprobe")
    require_tools(*found)
    return found


def run(ffmpeg, *args):
    return command([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *args], timeout=75).stdout


@pytest.fixture(scope="module")
def clips(tmp_path_factory, tools):
    ffmpeg, _ = tools
    root = tmp_path_factory.mktemp("originals")
    paths = []
    for i, (width, height, rate) in enumerate(((192, 108, 24), (192, 108, 30), (162, 162, 60), (192, 108, 25), (108, 192, 30))):
        path = root / f"clip-{i}.mp4"
        source = f"color={COLORS[i]}:s={width}x{height}:r={rate}:d=7"
        flash = "drawbox=x=iw*7/16:y=ih*7/16:w=iw/8:h=ih/8:color=white:t=fill:enable='lt(mod(t+0.00001,0.5),0.08)'"
        if i == 3:
            flash += ",select='not(eq(mod(n,13),2))'"
        audio = ["-f", "lavfi", "-i", r"aevalsrc=if(lt(mod(t\,0.5)\,0.08)\,0.65\,0.12)*sin(2*PI*900*t):s=48000:d=7",
                 "-c:a", "aac"] if i != 2 else []
        encoded = root / "unrotated.mp4" if i == 1 else path
        run(ffmpeg, "-f", "lavfi", "-i", source, *audio, "-filter_threads", "1", "-vf", flash, "-fps_mode", "passthrough",
            "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_range", "tv", "-colorspace", "smpte170m", "-color_trc", "bt709", "-color_primaries", "bt709", encoded)
        if i == 1:
            run(ffmpeg, "-display_rotation:v:0", "90", "-i", encoded, "-c", "copy", path)
        paths.append(path)
    Image.new("RGBA", (72, 36), (32, 220, 180, 210)).save(root / "logo.png")
    return paths


def checksums(paths):
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


def pixels(ffmpeg, output, x, y):
    raw = run(ffmpeg, "-threads", "2", "-i", output, "-an", "-vf",
              f"crop=8:8:{x}:{y},scale=in_color_matrix=bt709,format=rgb24",
              "-fps_mode", "passthrough", "-threads", "1", "-f", "rawvideo", "-")
    return [tuple(statistics.mean(raw[i+c:i+192:3]) for c in range(3)) for i in range(0, len(raw), 192)]


def identity_errors(samples, order, starts, transitions):
    failures = []
    for frame, rgb in enumerate(samples):
        i = max(i for i, start in enumerate(starts) if frame >= start)
        color = RGB[order[i]]
        if i and (overlap := transitions[i-1]["frames"]) and frame < starts[i] + overlap:
            r = (frame - starts[i])/overlap
            color = tuple(a*(1-r) + b*r for a, b in zip(RGB[order[i-1]], color))
        if max(abs(x-y) for x, y in zip(color, rgb)) > 15:
            failures.append(frame)
    return failures


def assert_audio_markers(ffmpeg, output, spec, order):
    raw = run(ffmpeg, "-threads", "2", "-i", output, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-")
    samples = array("f")
    samples.frombytes(raw)
    starts, frames = layout(spec)
    center = pixels(ffmpeg, output, 536, 956)
    assert abs(len(samples) - frames * 1600) <= 1600
    for i, (start, length, clip) in enumerate(zip(starts, lengths(spec), spec["clips"])):
        before = spec["transitions"][i-1]["frames"] if i else 0
        after = spec["transitions"][i]["frames"] if i < 4 else 0
        interior = range(start + before + 2, start + length - after - 2)
        energy = lambda n: max(abs(value) for value in samples[n*1600+300:n*1600+1300])
        if order[i] == 2:
            assert max(energy(n) for n in interior) < 0.001
        else:
            for n in (interior.start, interior.stop - 1):
                assert energy(n) > 0.035
            if order[i] in (1, 4):
                visual_starts = [n for n in interior if min(center[n]) > 200 >= min(center[n-1])]
                audio_starts = [n for n in interior if energy(n) > 0.28 >= energy(n-1)]
                assert len(visual_starts) >= 5 and len(audio_starts) >= 5
                assert all(min(abs(n - a) for a in audio_starts) <= 1 or n in (interior.start, interior.stop-1)
                           for n in visual_starts), (visual_starts, audio_starts, clip)


def test_default_title_rasterizes_unicode_glyphs():
    def glyph(text):
        image = title_image(text, "#FFFFFF", "#000000", None)
        ink = image.getchannel("R")
        assert (bounds := ink.getbbox()) is not None
        return ink.crop(bounds)

    missing = glyph("\U0010ffff")
    for character in "éñüç·—‘’“”:'\\%,":
        rendered = glyph(character)
        assert (rendered.size, rendered.tobytes()) != (missing.size, missing.tobytes()), character
    for accented, plain in zip("éñüç", "enuc"):
        assert glyph(accented) != glyph(plain)
    assert glyph("café") == glyph("cafe\u0301")


def test_dissolves_audio_rotation_and_originals(clips, tools, tmp_path):
    ffmpeg, ffprobe = tools
    assert inspect_source(clips[1], ffprobe).portrait
    assert inspect_source(clips[2], ffprobe).audio is None
    before = checksums(clips)
    brand = {"text": "Five clips: café & craft", "backgroundColor": "#24364A", "textColor": "#F9C74F",
             "logo": {"path": str(clips[0].parent / "logo.png"), "position": "top-right", "width": 160}}
    spec = autoedit([str(p) for p in clips], root=tmp_path, ffprobe=ffprobe, branding=brand)
    assert spec == autoedit([str(p) for p in clips], root=tmp_path, ffprobe=ffprobe, branding=brand)
    assert len(spec["transitions"]) == 4 and all(t["frames"] == 18 for t in spec["transitions"])
    output = tmp_path / "kept.mp4"
    report = render(spec, output, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    print(f"Dissolve/keep: {report['renderSeconds']}s render, {report['validationSeconds']}s validation, {report['bytes']} bytes")
    assert report["video"] == VIDEO_PROFILE and report["fastStart"] and report["continuousTimestamps"]
    assert report["frames"] == 810 and report["audioDurationSeconds"] == pytest.approx(27, abs=1/30)
    assert checksums(clips) == before
    with pytest.raises(EngineError, match="already exists"):
        render(spec, clips[0], root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert checksums(clips) == before
    order = [int(Path(clip["path"]).stem[-1]) for clip in spec["clips"]]
    assert order == [1, 0, 2, 3, 4]
    starts, _ = layout(spec)
    samples = pixels(ffmpeg, output, 700, 964)
    assert len(samples) == 810
    assert not identity_errors(samples, order, starts, spec["transitions"])
    wrong = [starts[0], starts[1]+9, *starts[2:]]
    assert identity_errors(samples, order, wrong, spec["transitions"])
    padding = pixels(ffmpeg, output, 16, 16)
    assert padding[starts[1]+25] == pytest.approx((36, 54, 74), abs=5)
    assert_audio_markers(ffmpeg, output, spec, order)


def test_local_upload_cut_muted_preview_and_save(clips, tools, tmp_path):
    ffmpeg, ffprobe = tools
    original = checksums(clips)
    with LocalServer(0, tmp_path, ffmpeg, ffprobe) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"

            def request(path, method="GET", body=None, **headers):
                try:
                    return urlopen(Request(base+path, data=body, method=method, headers=headers), timeout=200)
                except HTTPError as exc:
                    raise AssertionError(f"HTTP {exc.code}: {exc.read().decode()}") from exc

            html = request("/").read().decode()
            assert 'id="video" controls playsinline' in html and 'Save MP4' in html
            token = {"X-Video-Engine-Token": server.token}
            job = json.load(request("/job", "POST", **token))["job"]
            for path, name in zip(clips, CLIPS):
                assert json.load(request(f"/job/{job}/{name}", "PUT", path.read_bytes(), **token))["ok"]
            settings = {"transition": "cut", "audio": "mute", "fit": "cover", "text": "Second brand",
                        "backgroundColor": "#C52E51", "textColor": "#FFFFFF", "panelColor": "#C52E51"}
            report = json.load(request(f"/job/{job}/render", "POST", json.dumps(settings).encode(), **token))
            print(f"Cut/mute UI: {report['renderSeconds']}s render, {report['validationSeconds']}s validation, {report['bytes']} bytes")
            assert report["audio"] == "mute" and report["audioDurationSeconds"] is None and report["frames"] == 810
            with request(f"/job/{job}/preview", Range="bytes=0-15") as preview:
                assert preview.status == 206 and preview.headers["Content-Type"] == "video/mp4"
                assert len(preview.read()) == 16
            with request(f"/job/{job}/save") as download:
                assert download.headers["Content-Disposition"] == 'attachment; filename="finished-video.mp4"'
                output = tmp_path / "saved.mp4"
                output.write_bytes(download.read())
            assert output.stat().st_size == report["bytes"] and checksums(clips) == original
            starts = [0, *(join["startFrame"] for join in report["joins"])]
            assert not identity_errors(pixels(ffmpeg, output, 900, 935), [1, 0, 2, 3, 4], starts,
                                       [{"frames": 0} for _ in range(4)])
        finally:
            server.shutdown()
            worker.join()


def test_rejects_invalid_and_hdr_without_export(tools, tmp_path):
    ffmpeg, ffprobe = tools
    corrupt = tmp_path / "bad.mp4"
    corrupt.write_bytes(b"not video")
    with pytest.raises(EngineError, match="cannot read source"):
        inspect_source(corrupt, ffprobe)
    hdr = tmp_path / "hdr.mp4"
    run(ffmpeg, "-f", "lavfi", "-i", "color=green:s=96x96:d=1", "-c:v", "libx264", "-threads", "2",
        "-color_trc", "arib-std-b67", "-color_primaries", "bt2020", "-colorspace", "bt2020nc", hdr)
    with pytest.raises(EngineError, match="unsupported color profile"):
        inspect_source(hdr, ffprobe)


def test_matroska_longer_audio_does_not_extend_video(tools, tmp_path):
    ffmpeg, ffprobe = tools
    path = tmp_path / "audio-tail.mkv"
    run(ffmpeg, "-f", "lavfi", "-i", "color=s=96x96:r=30:d=5", "-f", "lavfi", "-i", "sine=d=8",
        "-c:v", "libx264", "-threads:v", "2", "-c:a", "aac", path)
    assert inspect_source(path, ffprobe).frames == 150


def test_exact_video_ticks_preserve_minimum_edit_length(tools, tmp_path):
    ffmpeg, ffprobe = tools
    counts = [151, 149, 150, 150, 150]
    paths = [tmp_path / f"clip-{i}.mp4" for i in range(5)]
    for path, count in zip(paths, counts):
        run(ffmpeg, "-f", "lavfi", "-i", "color=s=96x96:r=30", "-frames:v", str(count),
            "-c:v", "libx264", "-threads:v", "2", path)
    spec = autoedit(paths, root=tmp_path, ffprobe=ffprobe)
    assert lengths(spec) == counts
    assert layout(spec)[1] == 750


def test_cut_after_one_frame_opener_preserves_timeline(clips, tools, tmp_path):
    ffmpeg, ffprobe = tools
    spec = autoedit(clips, root=tmp_path, ffprobe=ffprobe, transition="cut", audio="mute")
    for clip, count in zip(spec["clips"], (1, 188, 188, 188, 188)):
        clip.update(inFrame=0, outFrame=count)
    output = tmp_path / "one-frame-opener.mp4"
    report = render(spec, output, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert report["continuousTimestamps"] and report["frames"] == 753
    assert report["joins"][0]["startFrame"] == 1
    samples = pixels(ffmpeg, output, 700, 964)
    assert not identity_errors(samples, [1, 0, 2, 3, 4], layout(spec)[0], spec["transitions"])

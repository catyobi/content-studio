import hashlib
import http.client
import json
import os
import select
import statistics
import struct
import subprocess
import threading
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image, ImageStat

from video_engine import media, server as web
from video_engine.branding import prepare_overlays, title_image
from video_engine.media import command, inspect_source, require_tools
from video_engine.planner import autoedit
from video_engine.renderer import render, require_hdr_filters
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


def identity_errors(samples, order, starts, transitions, *, tolerance=15):
    failures = []
    for frame, rgb in enumerate(samples):
        i = max(i for i, start in enumerate(starts) if frame >= start)
        color = RGB[order[i]]
        if i and (overlap := transitions[i-1]["frames"]) and frame < starts[i] + overlap:
            r = (frame - starts[i])/overlap
            color = tuple(a*(1-r) + b*r for a, b in zip(RGB[order[i-1]], color))
        if max(abs(x-y) for x, y in zip(color, rgb)) > tolerance:
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
    assert inspect_source(clips[1], ffprobe, ffmpeg=ffmpeg).portrait
    assert inspect_source(clips[2], ffprobe, ffmpeg=ffmpeg).audio is None
    before = checksums(clips)
    brand = {"text": "Five clips: café & craft", "backgroundColor": "#24364A", "textColor": "#F9C74F",
             "logo": {"path": str(clips[0].parent / "logo.png"), "position": "top-right", "width": 160}}
    spec = autoedit([str(p) for p in clips], root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe, branding=brand)
    assert spec == autoedit([str(p) for p in clips], root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe, branding=brand)
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


def test_local_upload_cut_muted_preview_and_save(clips, tools, tmp_path, monkeypatch):
    ffmpeg, ffprobe = tools
    original = checksums(clips)
    scans = []
    count_frames = media.decoded_frame_count

    def track_scan(path, *args):
        scans.append(path.name)
        return count_frames(path, *args)

    monkeypatch.setattr(media, "decoded_frame_count", track_scan)
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
            assert scans == list(CLIPS)
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


@pytest.fixture
def running_editor(tools, tmp_path):
    with LocalServer(0, tmp_path, *tools) as editor:
        worker = threading.Thread(target=editor.serve_forever)
        worker.start()
        try:
            yield editor
        finally:
            editor.shutdown()
            worker.join()


def editor_request(editor, method, route, body=None):
    with closing(http.client.HTTPConnection("127.0.0.1", editor.server_port, timeout=30)) as connection:
        connection.request(method, route, body, {"X-Video-Engine-Token": editor.token})
        response = connection.getresponse()
        return response.status, json.loads(response.read())


def test_upload_survives_short_pause_and_cleans_up_after_idle_timeout(running_editor, monkeypatch):
    editor = running_editor
    monkeypatch.setattr(web, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(web, "UPLOAD_IDLE_TIMEOUT", 1.0)
    status, reply = editor_request(editor, "POST", "/job")
    assert status == 201
    folder = editor.folder / reply["job"]
    route = f"/job/{reply['job']}/clip5"
    payload = b"video bytes" * 160_000

    for resume in (False, True):
        with closing(http.client.HTTPConnection("127.0.0.1", editor.server_port, timeout=5)) as connection:
            connection.putrequest("PUT", route)
            connection.putheader("X-Video-Engine-Token", editor.token)
            connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload[:1024**2])
            if resume:
                assert not select.select([connection.sock], [], [], 0.35)[0]
                connection.send(payload[1024**2:])
            response = connection.getresponse()
            reply = json.loads(response.read())
            if resume:
                assert response.status == 200 and reply == {"ok": True}
                assert (folder / "clip5").read_bytes() == payload
            else:
                assert response.status == 408
                assert reply["error"].startswith("Clip 5 upload: No data arrived")
                assert not (folder / "clip5").exists()
            assert not (folder / "clip5.partial").exists()


def test_sustained_upload_outlasts_both_timeouts(running_editor, monkeypatch):
    editor = running_editor
    monkeypatch.setattr(web, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(web, "UPLOAD_IDLE_TIMEOUT", 1.0)
    status, reply = editor_request(editor, "POST", "/job")
    assert status == 201
    folder = editor.folder / reply["job"]
    chunks = [bytes([i]) * 64 * 1024 for i in range(5)]
    payload = b"".join(chunks)

    def paced_body():
        yield chunks[0]
        for chunk in chunks[1:]:
            sleep(0.3)
            yield chunk

    with closing(http.client.HTTPConnection("127.0.0.1", editor.server_port, timeout=5)) as connection:
        started = monotonic()
        connection.request("PUT", f"/job/{reply['job']}/clip5", paced_body(), {
            "X-Video-Engine-Token": editor.token, "Content-Length": str(len(payload)),
        })
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"ok": True}
        assert monotonic() - started > web.UPLOAD_IDLE_TIMEOUT
    assert (folder / "clip5").read_bytes() == payload
    assert not list(folder.glob("*.partial"))


def test_close_interrupts_stalled_upload_and_keeps_completed_copy(running_editor, monkeypatch):
    editor = running_editor
    monkeypatch.setattr(web, "UPLOAD_IDLE_TIMEOUT", 5.0)
    status, reply = editor_request(editor, "POST", "/job")
    assert status == 201
    folder = editor.folder / reply["job"]
    route = f"/job/{reply['job']}/clip5"
    assert editor_request(editor, "PUT", route, b"original upload")[0] == 200

    queued = threading.Event()
    lock = editor.lock

    class ObservedLock:
        def __enter__(self):
            if not lock.acquire(blocking=False):
                queued.set()
                lock.acquire()

        def __exit__(self, *_):
            lock.release()

    monkeypatch.setattr(editor, "lock", ObservedLock())
    with (
        closing(http.client.HTTPConnection("127.0.0.1", editor.server_port, timeout=5)) as connection,
        closing(http.client.HTTPConnection("127.0.0.1", editor.server_port, timeout=5)) as queued_connection,
    ):
        connection.putrequest("PUT", route)
        connection.putheader("X-Video-Engine-Token", editor.token)
        connection.putheader("Content-Length", "2048")
        connection.endheaders(b"x" * 1024)
        deadline = monotonic() + 2
        while not (folder / "clip5.partial").exists():
            assert monotonic() < deadline, "The upload did not start."
            sleep(0.01)
        queued_connection.putrequest("PUT", f"/job/{reply['job']}/clip4")
        queued_connection.putheader("X-Video-Engine-Token", editor.token)
        queued_connection.putheader("Content-Length", "2048")
        queued_connection.endheaders(b"y" * 1024)
        assert queued.wait(2), "The second upload did not contend for the job lock."

        started = monotonic()
        editor.shutdown()
        editor.server_close()
        assert monotonic() - started < 2
        assert queued_connection.getresponse().status == 503
        assert not list(folder.glob("*.partial"))
        assert not (folder / "clip4").exists()
        assert (folder / "clip5").read_bytes() == b"original upload"


@pytest.mark.parametrize(("option", "stage", "limit"), [("-show_streams", "Probing", 45), ("-progress", "Decoding", 180)])
def test_source_timeout_reports_clip_and_processing_stage(clips, running_editor, monkeypatch, option, stage, limit):
    editor = running_editor
    status, reply = editor_request(editor, "POST", "/job")
    assert status == 201
    job = f"/job/{reply['job']}"
    for name, clip in zip(CLIPS, clips):
        assert editor_request(editor, "PUT", f"{job}/{name}", clip.read_bytes())[0] == 200

    entered, release = threading.Event(), threading.Event()
    run_command = subprocess.run

    def intercept(args, **kwargs):
        if option in args and any(isinstance(item, Path) and item.name == "clip5" for item in args):
            assert kwargs["timeout"] == limit
            entered.set()
            assert release.wait(10)
            raise subprocess.TimeoutExpired(args, limit)
        return run_command(args, **kwargs)

    monkeypatch.setattr(media.subprocess, "run", intercept)
    with ThreadPoolExecutor() as executor:
        render_request = executor.submit(editor_request, editor, "POST", f"{job}/render", b"{}")
        try:
            assert entered.wait(10)
            assert editor_request(editor, "GET", f"{job}/status") == (200, {"progress": f"{stage} clip 5 of 5"})
        finally:
            release.set()
        status, reply = render_request.result()
    assert status == 422
    assert reply["error"].startswith(f"{stage} clip 5 of 5 failed:")
    assert f"exceeded the {limit}-second time limit" in reply["error"]
    assert editor_request(editor, "GET", f"{job}/status") == (200, {"progress": None})


@pytest.mark.parametrize("audio", ["keep", "mute"])
def test_mixed_joins_export(clips, tools, tmp_path, audio):
    ffmpeg, ffprobe = tools
    spec = autoedit(clips, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe, audio=audio)
    for i in (0, 2):
        spec["transitions"][i] = {"type": "cut", "frames": 0}
    output = tmp_path / "mixed.mp4"
    report = render(spec, output, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert report["video"] == VIDEO_PROFILE and report["fastStart"] and report["continuousTimestamps"]
    assert report["frames"] == 846 and report["durationSeconds"] == pytest.approx(28.2)
    assert report["audio"] == audio
    assert [(join["type"], join["startFrame"], join["firstFullyIncomingFrame"]) for join in report["joins"]] == [
        ("cut", 177, 177), ("dissolve", 336, 354), ("cut", 512, 512), ("dissolve", 670, 688),
    ]
    samples = pixels(ffmpeg, output, 700, 964)
    assert len(samples) == 846
    order = [1, 0, 2, 3, 4]
    starts, _ = layout(spec)
    assert not identity_errors(samples, order, starts, spec["transitions"], tolerance=8)
    for i in range(1, len(starts)):
        delayed = [*starts]
        delayed[i] += 1
        assert identity_errors(samples, order, delayed, spec["transitions"], tolerance=8)
    if audio == "keep":
        assert report["audioDurationSeconds"] == pytest.approx(28.2, abs=1/30)
        assert_audio_markers(ffmpeg, output, spec, order)
    else:
        assert report["audioDurationSeconds"] is None


def test_renders_mixed_sdr_hlg_and_hdr10_with_matching_colors(tools, tmp_path):
    ffmpeg, ffprobe = tools
    colors = [(0, 0, 0), (115, 115, 115), (230, 230, 230), (255, 255, 255), (255, 255, 255),
              (186, 124, 98), (186, 67, 57), (53, 176, 76), (63, 97, 185), (30, 30, 30)]
    patches = [tuple((value / 255) ** 2.4 for value in color) for color in colors]
    patches[3:5] = [(1.2, 1.2, 1.2), (8.0, 8.0, 8.0)]
    pattern = tmp_path / "patches.rgb"
    samples = [patches[5 * (y // 48) + x // 36][channel]
               for channel in (1, 2, 0) for y in range(96) for x in range(180)]
    pattern.write_bytes(struct.pack(f"<{len(samples)}f", *samples))

    paths = [tmp_path / name for name in ("sdr.mp4", "hlg.mov", "pq.mp4", "odd-hlg.webm", "sdr2.mp4")]
    for path, transfer in zip(paths[:4], ("bt709", "arib-std-b67", "smpte2084", "arib-std-b67")):
        hdr = transfer != "bt709"
        odd = path.suffix == ".webm"
        primaries, matrix = ("bt2020", "bt2020nc") if hdr else ("bt709", "bt709")
        pixel_format = "yuv420p10le" if hdr else "yuv420p"
        params = "pools=1:frame-threads=1:lossless=1"
        if transfer == "smpte2084":
            params += ":max-cll=800,300:master-display=G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)"
        encoder = "libx265" if hdr else "libx264"
        compression = ["-x265-params", params, "-tag:v", "hvc1"] if hdr else ["-qp", "0"]
        compression += ["-preset", "ultrafast"]
        crop = ""
        if odd:
            encoder, compression = "libvpx-vp9", ["-profile:v", "2", "-lossless", "1"]
            crop = ",crop=179:95:exact=1"
        run(ffmpeg, "-stream_loop", "-1", "-f", "rawvideo", "-pixel_format", "gbrpf32le", "-video_size", "180x96",
            "-framerate", "30", "-i", pattern, "-t", "6", "-filter_threads", "1", "-vf",
            f"zscale=pin=bt709:tin=linear:min=gbr:rin=full:p={primaries}:t={transfer}:m={matrix}:r=limited:npl=100,"
            f"format={pixel_format}{crop}", "-c:v", encoder, "-threads", "2",
            "-color_primaries", primaries, "-color_trc", transfer, "-colorspace", matrix, "-color_range", "tv",
            *compression, path)
    for copy in paths[4:]:
        copy.write_bytes(paths[0].read_bytes())
    original = checksums(paths)
    source_cache = {}
    spec = autoedit(paths, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe, audio="mute", source_cache=source_cache)
    sources = list(source_cache.values())
    assert sources[0].hdr_transfer is None
    assert (sources[1].hdr_transfer, sources[1].hdr_peak_nits) == ("arib-std-b67", 1000)
    assert (sources[2].hdr_transfer, sources[2].hdr_peak_nits) == ("smpte2084", 800)
    assert (sources[3].hdr_transfer, sources[3].width, sources[3].height) == ("arib-std-b67", 179, 95)
    assert media.probe(paths[3], "-show_streams", ffprobe=ffprobe)["streams"][0]["pix_fmt"] == "yuv420p10le"

    output = tmp_path / "mixed.mp4"
    report = render(spec, output, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe, source_cache=source_cache)
    assert report["video"] == VIDEO_PROFILE and report["frames"] == 810
    assert checksums(paths) == original

    starts, _ = layout(spec)
    select_frames = "+".join(f"eq(n,{start + 60})" for start in starts[:4])
    raw = run(ffmpeg, "-threads", "2", "-i", output, "-an", "-vf",
              f"select='{select_frames}',scale=in_color_matrix=bt709,format=rgb24",
              "-fps_mode", "passthrough", "-threads", "1", "-f", "rawvideo", "-")
    frame_bytes = 1080 * 1920 * 3
    assert len(raw) == 4 * frame_bytes
    observed = []
    for offset in range(0, len(raw), frame_bytes):
        frame = Image.frombytes("RGB", (1080, 1920), raw[offset:offset + frame_bytes])
        anchors = [(104 + 216 * (i % 5), 812 + 288 * (i // 5)) for i in range(len(colors))]
        observed.append([tuple(ImageStat.Stat(frame.crop((x, y, x + 8, y + 8))).mean) for x, y in anchors])
    print(f"Color patches SDR/HLG/PQ/odd HLG: {observed}; mixed render: {report['renderSeconds']}s")
    for actual in observed:
        for i, rgb in enumerate(actual):
            if len(set(colors[i])) == 1:
                assert max(rgb) - min(rgb) <= 4
            assert rgb == pytest.approx(colors[i], abs=16)
    for actual in observed[1:]:
        assert max(actual[0]) <= 4 and min(actual[4]) >= 250
        assert min(actual[4]) - max(actual[3]) >= 4
        for tone_mapped, sdr in zip(actual, observed[0]):
            assert tone_mapped == pytest.approx(sdr, abs=12)


def test_reports_missing_hdr_filters(monkeypatch):
    def without_zscale(args):
        return subprocess.CompletedProcess(args, 0, stdout=b" .S. tonemap V->V filter\n")

    monkeypatch.setattr("video_engine.renderer.command", without_zscale)
    with pytest.raises(EngineError, match="zscale and tonemap.*ffmpeg-full"):
        require_hdr_filters("ffmpeg")


def test_rejects_invalid_and_unsupported_color_profile_without_export(tools, tmp_path):
    ffmpeg, ffprobe = tools
    corrupt = tmp_path / "bad.mp4"
    corrupt.write_bytes(b"not video")
    with pytest.raises(EngineError, match="cannot read source"):
        inspect_source(corrupt, ffprobe, ffmpeg=ffmpeg)
    hdr = tmp_path / "hdr.mp4"
    run(ffmpeg, "-f", "lavfi", "-i", "color=green:s=96x96:d=1", "-vf",
        "setparams=color_primaries=bt709:color_trc=arib-std-b67:colorspace=bt709",
        "-c:v", "libx264", "-threads", "2", hdr)
    with pytest.raises(EngineError, match="unsupported color profile"):
        inspect_source(hdr, ffprobe, ffmpeg=ffmpeg)


def test_matroska_longer_audio_does_not_extend_video(tools, tmp_path):
    ffmpeg, ffprobe = tools
    path = tmp_path / "audio-tail.mkv"
    run(ffmpeg, "-f", "lavfi", "-i", "color=s=96x96:r=30:d=5", "-f", "lavfi", "-i", "sine=d=8",
        "-c:v", "libx264", "-threads:v", "2", "-c:a", "aac", path)
    assert inspect_source(path, ffprobe, ffmpeg=ffmpeg).frames == 150


def test_source_cache_invalidates_same_size_replacement(clips, tools, tmp_path):
    ffmpeg, ffprobe = tools
    path = tmp_path / "clip.mp4"
    path.write_bytes(clips[0].read_bytes())
    cache = {}

    def inspect():
        return inspect_source(path, ffprobe, ffmpeg=ffmpeg, source_cache=cache)

    original = inspect()
    assert inspect() is original
    before = path.stat()
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    updated = inspect()
    assert updated is not original
    assert inspect() is updated


def test_exact_video_ticks_preserve_minimum_edit_length(tools, tmp_path):
    ffmpeg, ffprobe = tools
    counts = [151, 149, 150, 150, 150]
    paths = [tmp_path / f"clip-{i}.mp4" for i in range(5)]
    for path, count in zip(paths, counts):
        run(ffmpeg, "-f", "lavfi", "-i", "color=s=96x96:r=30", "-frames:v", str(count),
            "-c:v", "libx264", "-threads:v", "2", path)
    spec = autoedit(paths, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert lengths(spec) == counts
    assert layout(spec)[1] == 750


def test_short_mkv_and_webm_supply_150_edit_frames(tools, tmp_path):
    ffmpeg, ffprobe = tools
    paths = []
    for fps, suffix, codec in ((24, "mkv", "libx264"), (60, "mkv", "libx264"),
                               (24, "webm", "libvpx-vp9"), (60, "webm", "libvpx-vp9"), (30, "mp4", "libx264")):
        path = tmp_path / f"clip-{fps}.{suffix}"
        run(ffmpeg, "-f", "lavfi", "-i", f"color=s=96x96:r={fps}:d=5", "-c:v", codec, "-threads:v", "2", path)
        paths.append(path)
    assert [inspect_source(path, ffprobe, ffmpeg=ffmpeg).frames for path in paths] == [150] * 5
    spec = autoedit(paths, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert layout(spec)[1] == 750


def test_stream_copy_edit_list_does_not_overcount(tools, tmp_path):
    ffmpeg, ffprobe = tools
    original, trimmed = (tmp_path / name for name in ("original.mp4", "trimmed.mp4"))
    run(ffmpeg, "-f", "lavfi", "-i", "testsrc2=s=96x96:r=24:d=8", "-c:v", "libx264",
        "-threads:v", "2", "-g", "48", original)
    run(ffmpeg, "-ss", "0.26", "-i", original, "-t", "5", "-c", "copy", "-use_editlist", "1", trimmed)
    assert b"elst" in trimmed.read_bytes()
    source = inspect_source(trimmed, ffprobe, ffmpeg=ffmpeg)
    assert source.frames == 156
    assert source.duration == Fraction(26, 5)


def test_cut_after_one_frame_opener_preserves_timeline(clips, tools, tmp_path):
    ffmpeg, ffprobe = tools
    spec = autoedit(clips, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe, transition="cut", audio="mute")
    for clip, count in zip(spec["clips"], (1, 188, 188, 188, 188)):
        clip.update(inFrame=0, outFrame=count)
    output = tmp_path / "one-frame-opener.mp4"
    report = render(spec, output, root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert report["continuousTimestamps"] and report["frames"] == 753
    assert report["joins"][0]["startFrame"] == 1
    samples = pixels(ffmpeg, output, 700, 964)
    assert not identity_errors(samples, [1, 0, 2, 3, 4], layout(spec)[0], spec["transitions"])


def test_opener_uses_rotated_display_aspect_ratio(clips, tools, tmp_path):
    ffmpeg, ffprobe = tools
    path = tmp_path / "anamorphic.mp4"
    run(ffmpeg, "-f", "lavfi", "-i", "color=s=192x108:r=30:d=3", "-vf", "setsar=1/3",
        "-c:v", "libx264", "-threads:v", "2", path)
    rotated = tmp_path / "rotated.mp4"
    run(ffmpeg, "-display_rotation:v:0", "90", "-i", path, "-c", "copy", rotated)
    assert inspect_source(path, ffprobe, ffmpeg=ffmpeg).portrait
    assert not inspect_source(rotated, ffprobe, ffmpeg=ffmpeg).portrait
    spec = autoedit([clips[0], path, *clips[2:]], root=tmp_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert spec["clips"][0]["path"] == path.name


def test_logo_honors_exif_orientation(tmp_path):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6
    source = Image.new("RGB", (80, 40), "red")
    source.paste("blue", (40, 0, 80, 40))
    source.save(path, exif=exif)
    brand = {"logo": {"path": path.name, "position": "top-right", "width": 80}, "textOverlays": []}
    overlay, *_ = prepare_overlays(brand, tmp_path, tmp_path)[0]
    with Image.open(overlay) as logo:
        assert logo.size == (40, 80)
        assert logo.getpixel((20, 20)) == pytest.approx((255, 0, 0, 255), abs=5)
        assert logo.getpixel((20, 60)) == pytest.approx((0, 0, 255, 255), abs=5)

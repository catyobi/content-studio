import re
import struct
from fractions import Fraction

from .media import command, fraction, probe
from .timeline import FPS, EngineError, layout, lengths

VIDEO_PROFILE = {
    "codec_name": "h264", "profile": "High", "level": 40, "width": 1080, "height": 1920,
    "pix_fmt": "yuv420p", "sample_aspect_ratio": "1:1", "display_aspect_ratio": "9:16",
    "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "color_range": "tv", "color_space": "bt709",
    "color_transfer": "bt709", "color_primaries": "bt709",
}


def top_level_boxes(path):
    boxes = []
    limit = path.stat().st_size
    with path.open("rb") as handle:
        while (offset := handle.tell()) < limit:
            header = handle.read(8)
            if len(header) != 8:
                raise EngineError("Incomplete MP4 box header.")
            size, kind = struct.unpack(">I4s", header)
            minimum = 8
            if size == 1:
                extended_size = handle.read(8)
                if len(extended_size) != 8:
                    raise EngineError("Incomplete extended MP4 box size.")
                size, = struct.unpack(">Q", extended_size)
                minimum = 16
            if size == 0:
                size = limit - offset
            if size < minimum or offset + size > limit:
                raise EngineError("Invalid MP4 box size.")
            boxes.append(kind)
            handle.seek(offset + size)
    return boxes


def assert_branches(spec, log):
    for i, count in enumerate(lengths(spec)):
        prefix = rf"\[showinfo@clip{i} @ [^\]]+\]"
        config = re.search(prefix + r" config in time_base: 1/30, frame_rate: 30/1\b", log)
        frames = re.findall(prefix + r" n:\s*(\d+) pts:\s*(-?\d+).*fmt:yuv444p\b[^\n]*sar:1/1 s:1080x1920\b", log)
        if not config or [(int(n), int(pts)) for n, pts in frames] != [(n, n) for n in range(count)]:
            raise EngineError(f"Clip {i+1} did not supply {count} continuous normalized frames; check its trim and stream duration.")


def artifacts(path, ffmpeg):
    result = command([ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-threads", "2", "-i", path, "-an",
                      "-vf", "blackdetect=d=0.066:pix_th=0.10:pic_th=0.98,freezedetect=n=-60dB:d=0.10",
                      "-fps_mode", "passthrough", "-threads", "1", "-f", "framemd5", "-"], timeout=90)
    log = result.stderr.decode(errors="replace")
    hashes = [line.rsplit(b",", 1)[-1].strip() for line in result.stdout.splitlines() if not line.startswith(b"#")]
    return {"blackStartsSeconds": [float(value) for value in re.findall(r"black_start:([\d.]+)", log)],
            "freezeStartsSeconds": [float(value) for value in re.findall(r"freeze_start:\s*([\d.]+)", log)],
            "exactDuplicateFrames": [i for i in range(1, len(hashes)) if hashes[i] == hashes[i-1]]}, len(hashes)


def verify(path, spec, *, ffmpeg, ffprobe):
    starts, frames = layout(spec)
    info = probe(path, "-show_streams", "-show_format", ffprobe=ffprobe)
    streams = info.get("streams", [])
    videos = [s for s in streams if s["codec_type"] == "video"]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    keep = spec["audio"] == "keep"
    if len(videos) != 1 or len(audios) != int(keep) or len(streams) != 1 + int(keep):
        raise EngineError("Export stream count does not match the requested audio policy.")
    video = videos[0]
    wrong = [k for k, value in VIDEO_PROFILE.items() if video.get(k) != value]
    if wrong or any(item.get("rotation") for item in video.get("side_data_list", [])):
        raise EngineError(f"Export metadata failed validation: {', '.join(wrong) or 'unexpected rotation'}.")
    boxes = top_level_boxes(path)
    if b"moov" not in boxes or b"mdat" not in boxes or boxes.index(b"moov") > boxes.index(b"mdat"):
        raise EngineError("Export is not a fast-start MP4 (expected moov before mdat).")

    expected = Fraction(frames, FPS)
    duration = fraction(info.get("format", {}).get("duration"), Fraction(0))
    if not 25 <= duration <= 30 or abs(duration - expected) > Fraction(1, FPS):
        raise EngineError("Export duration does not match the 25–30 second timeline.")
    records = probe(path, "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp",
                    ffprobe=ffprobe).get("frames", [])
    clock = fraction(video.get("time_base"), Fraction(0))
    if len(records) != frames or any(fraction(record.get("best_effort_timestamp"), -1) * clock != Fraction(i, FPS)
                                    for i, record in enumerate(records)):
        raise EngineError("Export did not preserve continuous frame timestamps; a join may have lost a frame.")

    audio_seconds = None
    if keep:
        stream = audios[0]
        audio_duration = fraction(stream.get("duration"), Fraction(0))
        audio_start = fraction(stream.get("start_time"), Fraction(99))
        if (stream.get("codec_name") != "aac" or stream.get("sample_rate") != "48000" or stream.get("channels") != 2
                or abs(audio_start) > Fraction(1, FPS) or abs(audio_start + audio_duration - expected) > Fraction(1, FPS)):
            raise EngineError("Export audio format or presentation duration does not match the timeline.")
        audio_seconds = float(audio_duration)

    advisories, decoded = artifacts(path, ffmpeg)
    if decoded != frames:
        raise EngineError("The decoded frame count does not match the timeline.")
    joins = [{"startFrame": start, "firstFullyIncomingFrame": start + transition["frames"], "type": transition["type"]}
             for start, transition in zip(starts[1:], spec["transitions"])]
    return {"frames": frames, "durationSeconds": float(duration), "video": VIDEO_PROFILE.copy(),
            "fastStart": True, "continuousTimestamps": True, "normalizedClipFrames": lengths(spec),
            "joins": joins, "audio": spec["audio"], "audioDurationSeconds": audio_seconds, "advisories": advisories}

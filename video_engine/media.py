import json
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .timeline import FPS, EngineError

MAX_BYTES = 1024**3
MAX_SECONDS = 180
UNKNOWN = (None, "unknown", "unspecified", "reserved")


def command(args, *, timeout=45, capture=True, **kwargs):
    try:
        return subprocess.run(args, check=True, timeout=timeout, capture_output=capture, **kwargs)
    except FileNotFoundError as exc:
        raise EngineError(f"Missing executable {args[0]!s}; install FFmpeg and ffprobe 7 or newer.") from exc
    except subprocess.TimeoutExpired as exc:
        raise EngineError(f"{Path(args[0]).name} exceeded the {timeout}-second time limit.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise EngineError(f"{Path(args[0]).name} exited with status {exc.returncode}: {stderr[-1200:].strip()}") from exc


def require_tools(ffmpeg, ffprobe):
    for binary in (ffmpeg, ffprobe):
        version = command([binary, "-version"]).stdout.decode().splitlines()[0]
        match = re.search(r"\bversion\s+(\d+)", version)
        if not match or int(match[1]) < 7:
            raise EngineError(f"FFmpeg and ffprobe 7 or newer are required (found {version}).")


def probe(path, *options, ffprobe="ffprobe"):
    result = command([ffprobe, "-v", "error", "-protocol_whitelist", "file", *options, "-of", "json", path])
    return json.loads(result.stdout)


def fraction(value, fallback=None):
    try:
        return Fraction(value)
    except (ValueError, TypeError, ZeroDivisionError):
        return fallback


def packet_duration(path, video, fps, ffprobe):
    clock = fraction(video.get("time_base"))
    if not clock or clock <= 0:
        return None
    packets = probe(path, "-select_streams", str(video["index"]), "-show_packets",
                    "-show_entries", "packet=pts,duration", ffprobe=ffprobe).get("packets", [])
    spans = []
    for packet in packets:
        pts = fraction(packet.get("pts"))
        if pts is not None:
            duration = fraction(packet.get("duration"), 0) * clock
            start = pts * clock
            spans.append((start, start + (duration if duration > 0 else 1 / fps)))
    if spans:
        return max(end for _, end in spans) - min(start for start, _ in spans)
    return None


@dataclass(frozen=True)
class Source:
    path: Path
    video: int
    audio: int | None
    width: int
    height: int
    rotation: int
    duration: Fraction
    start: Fraction
    matrix: str
    range: str
    warning: str | None = None
    sar: Fraction = Fraction(1)

    @property
    def frames(self):
        return int(self.duration * FPS)

    @property
    def portrait(self):
        display_width = self.width * self.sar
        return display_width > self.height if self.rotation in (90, 270) else self.height > display_width


def inspect_source(path, ffprobe="ffprobe"):
    if path.stat().st_size > MAX_BYTES:
        raise EngineError(f"{path.name}: source exceeds 1 GiB.")
    try:
        info = probe(path, "-show_streams", "-show_format", ffprobe=ffprobe)
    except EngineError as exc:
        raise EngineError(f"{path.name}: cannot read source ({exc}).") from exc
    streams = info.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s["index"] for s in streams if s["codec_type"] == "audio"), None)
    if not video or video["codec_name"] not in ("h264", "hevc", "vp8", "vp9", "av1"):
        raise EngineError(f"{path.name}: expected an H.264, HEVC, VP8, VP9, or AV1 video stream.")
    container = info.get("format", {})
    if not set(container.get("format_name", "").split(",")) & {"mov", "mp4", "matroska", "webm"}:
        raise EngineError(f"{path.name}: expected a MOV, MP4, MKV or WebM container.")
    fps = fraction(video.get("avg_frame_rate")) or fraction(video.get("r_frame_rate"))
    width, height = video["width"], video["height"]
    if min(width, height) < 16 or max(width, height) > 3840 or width * height > 3840 * 2160:
        raise EngineError(f"{path.name}: dimensions exceed the supported 4K pixel budget (or are below 16 × 16).")
    rotation = next((item["rotation"] for item in video.get("side_data_list", []) if "rotation" in item), 0) % 360
    if rotation not in (0, 90, 180, 270):
        raise EngineError(f"{path.name}: only right-angle rotation metadata is supported.")
    primaries, transfer, space = (video.get(key) for key in ("color_primaries", "color_transfer", "color_space"))
    matrices = {"bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601"}
    if (primaries not in (*UNKNOWN, "bt709") or transfer not in (*UNKNOWN, "bt709")
            or space not in (*UNKNOWN, *matrices)):
        raise EngineError(f"{path.name}: unsupported color profile ({primaries}/{transfer}/{space}); use SDR BT.709 footage.")
    ticks = fraction(video.get("duration_ts"), 0)
    duration = ticks * fraction(video.get("time_base"), 0)
    if not duration and fps and 0 < fps <= 60:
        try:
            duration = packet_duration(path, video, fps, ffprobe)
        except EngineError as exc:
            raise EngineError(f"{path.name}: cannot measure video duration ({exc}).") from exc
    if not duration or not 0 < duration <= MAX_SECONDS or not fps or not 0 < fps <= 60:
        raise EngineError(f"{path.name}: expected a known duration of 0–180 seconds and up to 60 fps.")
    color_range = video.get("color_range")
    full = color_range == "pc" or video.get("pix_fmt", "").startswith("yuvj")
    warning = f"{path.name}: untagged color fields were assumed to be SDR BT.709 video." if any(
        value in UNKNOWN for value in (primaries, transfer, space, color_range)
    ) else None
    start = fraction(video.get("start_time"), fraction(container.get("start_time"), Fraction(0)))
    sar = fraction((video.get("sample_aspect_ratio") or "").replace(":", "/"), Fraction(1))
    return Source(path, video["index"], audio, width, height, rotation, duration, start,
                  matrices.get(space, "bt709"), "pc" if full else "tv", warning, sar=sar if sar > 0 else Fraction(1))

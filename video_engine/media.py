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
NORMALIZE_CLOCK = f"setpts=PTS-STARTPTS,fps={FPS}"
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")
DEFAULT_HDR_PEAK_NITS = 1000


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


def hdr10_peak(path, video, ffprobe):
    info = probe(path, "-select_streams", str(video["index"]), "-read_intervals", "%+#24",
                 "-show_frames", "-show_entries", "frame=side_data_list", ffprobe=ffprobe)
    side_data = [*video.get("side_data_list", []),
                 *(item for frame in info.get("frames", []) for item in frame.get("side_data_list", []))]
    for field in ("max_content", "max_luminance"):
        for item in side_data:
            peak = fraction(item.get(field))
            if peak is not None and 100 < peak <= 10000:
                return float(peak)
    return DEFAULT_HDR_PEAK_NITS


def decoded_frame_count(path, stream, ffmpeg):
    result = command([ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-copyts",
                      "-progress", "pipe:1", "-threads", "2", "-protocol_whitelist", "file", "-i", path,
                      "-map", f"0:{stream}", "-filter_threads", "2", "-vf", NORMALIZE_CLOCK,
                      "-fps_mode", "passthrough", "-frames:v", str(MAX_SECONDS * FPS + 1), "-f", "null", "-"],
                     timeout=180)
    counts = re.findall(rb"^frame= *([0-9]+)$", result.stdout, re.MULTILINE)
    return int(counts[-1]) if counts else 0


@dataclass(frozen=True)
class Source:
    path: Path
    video: int
    audio: int | None
    width: int
    height: int
    rotation: int
    frames: int
    start: Fraction
    matrix: str
    range: str
    warning: str | None = None
    sar: Fraction = Fraction(1)
    hdr_transfer: str | None = None
    hdr_peak_nits: float | None = None
    pixel_format: str | None = None
    chroma_location: str | None = None

    @property
    def duration(self):
        return Fraction(self.frames, FPS)

    @property
    def portrait(self):
        display_width = self.width * self.sar
        return display_width > self.height if self.rotation in (90, 270) else self.height > display_width


def source_signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def inspect_source(path, ffprobe="ffprobe", *, ffmpeg="ffmpeg", source_cache=None, on_decode=None):
    stat = path.stat()
    if stat.st_size > MAX_BYTES:
        raise EngineError(f"{path.name}: source exceeds 1 GiB.")
    if source_cache is not None:
        signature = source_signature(stat)
        key = path, ffmpeg, ffprobe, signature
        if key in source_cache:
            return source_cache[key]
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
    if not fps or not 0 < fps <= 60:
        raise EngineError(f"{path.name}: expected a video frame rate of up to 60 fps.")
    width, height = video["width"], video["height"]
    if min(width, height) < 16 or max(width, height) > 3840 or width * height > 3840 * 2160:
        raise EngineError(f"{path.name}: dimensions exceed the supported 4K pixel budget (or are below 16 × 16).")
    rotation = next((item["rotation"] for item in video.get("side_data_list", []) if "rotation" in item), 0) % 360
    if rotation not in (0, 90, 180, 270):
        raise EngineError(f"{path.name}: only right-angle rotation metadata is supported.")
    primaries, transfer, space = (video.get(key) for key in ("color_primaries", "color_transfer", "color_space"))
    matrices = {"bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601"}
    hdr = primaries == "bt2020" and transfer in HDR_TRANSFERS and space == "bt2020nc"
    if not hdr and (primaries not in (*UNKNOWN, "bt709") or transfer not in (*UNKNOWN, "bt709")
                    or space not in (*UNKNOWN, *matrices)):
        raise EngineError(f"{path.name}: unsupported color profile ({primaries}/{transfer}/{space}); "
                          "use SDR BT.709 or BT.2020 HLG/HDR10 footage.")
    peak = hdr10_peak(path, video, ffprobe) if hdr and transfer == "smpte2084" else DEFAULT_HDR_PEAK_NITS
    if on_decode is not None:
        on_decode()
    try:
        frames = decoded_frame_count(path, video["index"], ffmpeg)
    except EngineError as exc:
        raise EngineError(f"{path.name}: cannot count video frames ({exc}).") from exc
    if not 0 < frames <= MAX_SECONDS * FPS:
        raise EngineError(f"{path.name}: expected decodable video of no more than {MAX_SECONDS} seconds.")
    color_range = video.get("color_range")
    full = color_range == "pc" or video.get("pix_fmt", "").startswith("yuvj")
    warning = f"{path.name}: untagged color fields were assumed to be SDR BT.709 video." if any(
        value in UNKNOWN for value in (primaries, transfer, space, color_range)
    ) else None
    if hdr and color_range in UNKNOWN:
        warning = f"{path.name}: untagged HDR range was assumed to be limited."
    start = fraction(video.get("start_time"), fraction(container.get("start_time"), Fraction(0)))
    sar = fraction((video.get("sample_aspect_ratio") or "").replace(":", "/"), Fraction(1))
    source = Source(path, video["index"], audio, width, height, rotation, frames, start,
                    matrices.get(space, "bt709"), "pc" if full else "tv", warning, sar=sar if sar > 0 else Fraction(1),
                    hdr_transfer=transfer if hdr else None, hdr_peak_nits=peak if hdr else None,
                    pixel_format=video.get("pix_fmt"), chroma_location=video.get("chroma_location"))
    if source_cache is not None:
        if source_signature(path.stat()) != signature:
            raise EngineError(f"{path.name}: source changed during inspection; try again.")
        source_cache[key] = source
    return source

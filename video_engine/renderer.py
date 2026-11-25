import os
import tempfile
from pathlib import Path
from time import perf_counter

from .branding import prepare_overlays
from .compiler import build_graph
from .media import command, inspect_source, require_tools
from .timeline import EngineError, asset_path, validate_spec
from .verify import assert_branches, verify


def render(spec, output, *, root, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    spec = validate_spec(spec)
    require_tools(ffmpeg, ffprobe)
    target = Path(output).absolute()
    if target.suffix.lower() != ".mp4" or not target.parent.is_dir():
        raise EngineError("Specify an .mp4 path in an existing output directory.")
    if target.exists() or target.is_symlink():
        raise EngineError("Output already exists; choose a new filename. Sources are never overwritten.")
    sources = [inspect_source(asset_path(root, item["path"]), ffprobe) for item in spec["clips"]]
    for i, (clip, source) in enumerate(zip(spec["clips"], sources), 1):
        if clip["outFrame"] > source.frames:
            raise EngineError(f"Clip {i} trim exceeds available media ({source.frames} normalized frames).")
    with tempfile.TemporaryDirectory(prefix=".video-engine-", dir=target.parent) as folder:
        scratch = Path(folder)
        overlays = prepare_overlays(spec["branding"], root, scratch)
        graph, audio = build_graph(spec, sources, overlays)
        inputs = []
        for path in [*(source.path for source in sources), *(item[0] for item in overlays)]:
            inputs += ["-threads", "2", "-protocol_whitelist", "file", "-i", path]
        pending = scratch / "export.mp4"
        log_path = scratch / "render.log"
        audio_flags = ["-map", f"[{audio}]", "-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
        args = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "info", "-copyts", *inputs,
                "-filter_complex_threads", "2", "-filter_complex", graph, "-map", "[out]", *audio_flags,
                "-map_metadata", "-1", "-map_chapters", "-1", "-fps_mode", "passthrough", "-c:v", "libx264",
                "-threads:v", "4", "-preset", "veryfast", "-crf", "22", "-maxrate", "16M", "-bufsize", "20M",
                "-profile:v", "high", "-level:v", "4.0", "-pix_fmt", "yuv420p", "-color_range", "tv",
                "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709", "-movflags", "+faststart", pending]
        started = perf_counter()
        with log_path.open("wb") as log:
            try:
                command(args, timeout=180, capture=False, stdout=log, stderr=log)
            except EngineError as exc:
                log.flush()
                with log_path.open("rb") as reader:
                    reader.seek(max(0, log_path.stat().st_size - 1600))
                    details = reader.read().decode(errors="replace").strip()
                raise EngineError(f"{exc}\n{details}".strip()) from exc
        render_time = perf_counter() - started
        started = perf_counter()
        assert_branches(spec, log_path.read_text(encoding="utf-8", errors="replace"))
        report = verify(pending, spec, ffmpeg=ffmpeg, ffprobe=ffprobe)
        try:
            os.link(pending, target)
        except FileExistsError as exc:
            raise EngineError("Output already exists; choose a new filename.") from exc
        except OSError as exc:
            raise EngineError(f"Unable to publish the output to this filesystem: {exc}") from exc
        report.update(renderSeconds=round(render_time, 2), validationSeconds=round(perf_counter()-started, 2),
                      bytes=target.stat().st_size, warnings=[source.warning for source in sources if source.warning])
        return report

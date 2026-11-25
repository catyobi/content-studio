import argparse
import json
import os
import sys
from pathlib import Path

from .planner import autoedit
from .renderer import render
from .timeline import EngineError, asset_path, load_spec


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local five-clip video editor")
    actions = parser.add_subparsers(dest="action", required=True)
    edit = actions.add_parser("autoedit", help="Plan an edit and write a versioned JSON timeline")
    edit.add_argument("clips", nargs=5)
    edit.add_argument("-o", "--output", type=Path, required=True)
    edit.add_argument("--transition", choices=("cut", "dissolve"), default="dissolve")
    edit.add_argument("--fit", choices=("contain", "cover"), default="contain")
    edit.add_argument("--audio", choices=("keep", "mute"), default="keep")
    edit.add_argument("--text", default="")
    edit.add_argument("--background-color", default="#102A43")
    edit.add_argument("--text-color", default="#F9C74F")
    edit.add_argument("--panel-color", default="#102A43")
    edit.add_argument("--logo")
    edit.add_argument("--logo-position", choices=("top-left", "top-right", "bottom-left", "bottom-right"), default="top-right")
    edit.add_argument("--logo-width", type=int, default=168)
    edit.add_argument("--font")
    export = actions.add_parser("render", help="Render and verify an MP4 from a JSON timeline")
    export.add_argument("timeline", type=Path)
    export.add_argument("-o", "--output", type=Path, required=True)
    ui = actions.add_parser("serve", help="Open the localhost-only editor")
    ui.add_argument("--port", type=int, default=8765)
    options = parser.parse_args(argv)
    ffmpeg = os.environ.get("VIDEO_ENGINE_FFMPEG", "ffmpeg")
    ffprobe = os.environ.get("VIDEO_ENGINE_FFPROBE", "ffprobe")
    try:
        if options.action == "serve":
            from .server import serve
            serve(options.port, ffmpeg=ffmpeg, ffprobe=ffprobe)
        elif options.action == "autoedit":
            root = options.output.absolute().parent
            brand = {"text": options.text, "backgroundColor": options.background_color,
                     "textColor": options.text_color, "panelColor": options.panel_color}
            if options.logo:
                brand["logo"] = {"path": os.path.relpath(asset_path(Path.cwd(), options.logo), root),
                                 "position": options.logo_position, "width": options.logo_width}
            if options.font:
                brand["fontFile"] = os.path.relpath(asset_path(Path.cwd(), options.font), root)
            spec = autoedit(options.clips, root=root, transition=options.transition, fit=options.fit,
                            audio=options.audio, branding=brand, ffmpeg=ffmpeg, ffprobe=ffprobe)
            with options.output.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(spec, indent=2) + "\n")
            print(options.output)
        else:
            report = render(load_spec(options.timeline), options.output, root=options.timeline.absolute().parent,
                            ffmpeg=ffmpeg, ffprobe=ffprobe)
            print(json.dumps(report, indent=2))
    except (EngineError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

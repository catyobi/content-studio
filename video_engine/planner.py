import os
from pathlib import Path

from .media import inspect_source
from .timeline import FPS, SIZE, EngineError, asset_path, layout, validate_spec


def balanced(capacities, target):
    parts = [0] * len(capacities)
    for _ in range(min(target, sum(capacities))):
        i = min((i for i, cap in enumerate(capacities) if parts[i] < cap), key=lambda i: (parts[i], i))
        parts[i] += 1
    return parts


def plan(sources, *, root, transition="dissolve", fit="contain", audio="keep", branding=None):
    if len(sources) != 5 or len({source.path for source in sources}) != 5:
        raise EngineError("Select exactly five distinct clips.")
    if transition not in ("cut", "dissolve"):
        raise EngineError("Transition must be cut or dissolve.")
    if any(source.frames < 15 for source in sources):
        raise EngineError("Each clip needs at least half a second of usable footage.")
    ordered = list(sources)
    opener = next((i for i, source in enumerate(ordered) if source.portrait and source.frames >= 90), 0)
    ordered.insert(0, ordered.pop(opener))
    capacities = [source.frames for source in ordered]
    if sum(capacities) < 750:
        raise EngineError("The five clips have less than 25 seconds of usable footage, even with cuts.")
    overlaps = [18 if transition == "dissolve" and min(a, b) >= 45 else 0
                for a, b in zip(capacities, capacities[1:])]
    room = sum(capacities) - 750
    for i, overlap in enumerate(overlaps):
        overlaps[i] = min(room, overlap) if room >= 2 else 0
        room -= overlaps[i]
    parts = balanced(capacities, 810 + sum(overlaps))
    clips = []
    for source, count in zip(ordered, parts):
        start = (source.frames - count) // 2
        clips.append({"path": os.path.relpath(source.path, root), "inFrame": start, "outFrame": start + count, "fit": fit})
    brand = {"backgroundColor": "#102A43", "textColor": "#F9C74F", "panelColor": "#102A43", "textOverlays": []}
    fields = dict(branding or {})
    text = fields.pop("text", "")
    brand.update(fields)
    spec = {"version": 1, "clips": clips, "transitions": [{"type": "dissolve" if n else "cut", "frames": n} for n in overlaps],
            "branding": brand, "audio": audio, "output": {"width": SIZE[0], "height": SIZE[1], "fps": FPS}}
    if text:
        brand["textOverlays"] = [{"text": text, "startFrame": 0, "endFrame": layout(spec)[1], "position": "bottom"}]
    return validate_spec(spec)


def autoedit(paths, *, root, ffmpeg="ffmpeg", ffprobe="ffprobe", source_cache=None, **options):
    if len(paths) != 5:
        raise EngineError("Select exactly five distinct clips.")
    sources = [inspect_source(asset_path(Path.cwd(), path), ffprobe, ffmpeg=ffmpeg, source_cache=source_cache)
               for path in paths]
    return plan(sources, root=root, **options)

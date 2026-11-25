import json
from pathlib import Path

from jsonschema import Draft202012Validator, validators

FPS = 30
SIZE = (1080, 1920)
SCHEMA = json.loads(Path(__file__).with_name("timeline.schema.json").read_text(encoding="utf-8"))
TimelineValidator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda checker, value: type(value) is int))


class EngineError(Exception):
    pass


def lengths(spec):
    return [clip["outFrame"] - clip["inFrame"] for clip in spec["clips"]]


def layout(spec):
    durations = lengths(spec)
    starts = [0]
    for previous, transition in zip(durations, spec["transitions"]):
        starts.append(starts[-1] + previous - transition["frames"])
    return starts, starts[-1] + durations[-1]


def validate_spec(spec):
    error = next(TimelineValidator(SCHEMA).iter_errors(spec), None)
    if error is not None:
        raise EngineError(f"Invalid timeline at {error.json_path}: {error.message}")
    overlaps = [0, *(item["frames"] for item in spec["transitions"]), 0]
    for i, (duration, before, after) in enumerate(zip(lengths(spec), overlaps, overlaps[1:]), 1):
        if duration <= before + after:
            raise EngineError(f"Clip {i} must have at least one exclusive frame after its overlaps.")
    _, total = layout(spec)
    if not 750 <= total <= 900:
        raise EngineError(f"Timeline is {total} frames; expected 750–900 frames (25–30 seconds).")
    for overlay in spec["branding"]["textOverlays"]:
        if not 0 <= overlay["startFrame"] < overlay["endFrame"] <= total:
            raise EngineError("Text overlay must have a nonempty range within the finished timeline.")
    return spec


def load_spec(path):
    try:
        return validate_spec(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as exc:
        raise EngineError(f"Cannot read timeline: {exc}") from exc


def asset_path(root, value):
    try:
        path = (root / value).resolve(strict=True)
        if not path.is_file():
            raise OSError("not a file")
        return path
    except (OSError, ValueError) as exc:
        raise EngineError(f"Cannot read asset {value!r}: {exc}") from exc

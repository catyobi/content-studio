import textwrap
from pathlib import Path
from unicodedata import normalize

from PIL import Image, ImageColor, ImageDraw, ImageFont, UnidentifiedImageError

from .timeline import EngineError, asset_path

DEFAULT_FONT = Path(__file__).with_name("fonts") / "NotoSans-Regular.ttf"


def title_image(text, color, panel_color, font_file):
    text = normalize("NFC", text)
    lines = "\n".join(textwrap.wrap(" ".join(text.split()), width=29))
    if not lines:
        raise EngineError("Overlay text must not be blank.")
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    for size in (54, 46, 38, 30, 24):
        font = ImageFont.truetype(font_file or DEFAULT_FONT, size)
        box = draw.multiline_textbbox((0, 0), lines, font=font, spacing=8)
        width, height = box[2] - box[0] + 40, box[3] - box[1] + 28
        if width <= 936 and height <= 300:
            title = Image.new("RGBA", (width, height), (*ImageColor.getrgb(panel_color), 200))
            ImageDraw.Draw(title).multiline_text((20 - box[0], 14 - box[1]), lines, fill=color, font=font, spacing=8)
            return title
    raise EngineError("Overlay text does not fit the supported title area.")


def prepare_overlays(brand, root, scratch):
    overlays = []
    try:
        if logo := brand.get("logo"):
            source = asset_path(root, logo["path"])
            if source.stat().st_size > 16 * 1024**2:
                raise EngineError("Logo must be 16 MiB or smaller.")
            with Image.open(source) as image:
                if image.format not in ("PNG", "JPEG", "WEBP") or image.width * image.height > 16_000_000:
                    raise EngineError("Use a PNG, JPEG or WebP logo of at most 16 megapixels.")
                image = image.convert("RGBA")
                image.thumbnail((logo["width"], 240), Image.Resampling.LANCZOS)
                path = scratch / "logo.png"
                image.save(path)
            side, edge = logo["position"].split("-")
            overlays.append((path, "72" if edge == "left" else "W-w-72", "112" if side == "top" else "H-h-112", ""))
        font_file = asset_path(root, brand["fontFile"]) if "fontFile" in brand else None
        if font_file and font_file.stat().st_size > 16 * 1024**2:
            raise EngineError("Font must be 16 MiB or smaller.")
        for i, item in enumerate(brand["textOverlays"]):
            title = title_image(item["text"], brand["textColor"], brand["panelColor"], font_file)
            path = scratch / f"title-{i}.png"
            title.save(path)
            overlays.append((path, "72", "370" if item["position"] == "top" else "H-h-370",
                             f":enable='gte(n,{item['startFrame']})*lt(n,{item['endFrame']})'"))
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise EngineError(f"Cannot prepare branding assets: {exc}") from exc
    return overlays

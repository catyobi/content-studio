# Local five-clip video editor

This proof of concept assembles five original clips into a branded 1080 × 1920 MP4. It runs on your computer and never overwrites an input. Preview is the finished MP4, not a simulated timeline. No accounts, network services, or vendor-specific infrastructure are needed for editing.

## Setup on an Apple Silicon Mac

Use a native `arm64` Terminal. Install [Homebrew](https://brew.sh/) if you do not already have it, then run these commands from the repository root:

```sh
brew install python@3.12 ffmpeg
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
ffmpeg -version
ffprobe -version
```

FFmpeg and ffprobe **7 or newer** are required, with `libx264` and FFmpeg's native AAC encoder enabled. The older FFmpeg 4.4 graph can lose timestamps at dissolve boundaries. On other platforms use Python 3.10+ and install an equivalent local FFmpeg build (Ubuntu 22.04's default apt FFmpeg is too old). If necessary, set `VIDEO_ENGINE_FFMPEG` and `VIDEO_ENGINE_FFPROBE` to the two executable paths. Use a maintained build and keep it updated.

## Select, render, preview and save

1. Start the editor with `python -m video_engine serve` and open **http://127.0.0.1:8765**. To use another local port, pass `--port 8766`.
2. Select exactly five clips in the multi-file picker. Pick an optional PNG, JPEG or WebP logo, a TTF/OTF font, a title and colors. Choose whole-shot framing or center crop, cuts or dissolves, and keep or mute source audio.
3. Choose **Render video**. Your browser copies inputs to a temporary folder on this computer. The server verifies the completed export before offering **Preview** and **Save MP4**. Open **Validation report** to see precise timings and diagnostic flags. Save before starting another job or stopping the server (`Ctrl-C`): starting a new job discards the previous local workspace, and the server removes its workspace on a normal exit.

The interface is bound to IPv4 loopback and handles one active browser job. It is not an authenticated, sandboxed multi-user service. Do not expose it on a network or give it untrusted media. Video files may be up to 1 GiB each, so allow room for copies plus the render.

### What the automatic edit does

- Moves the first portrait clip with at least three seconds of footage to the start (respecting rotation metadata); leaves the other clips in selection order. If none qualify, leaves the original order.
- Aims for 27 seconds. Distributes time as evenly as available footage permits and center-trims each clip, dropping equal amounts from its head and tail where possible. It does not score scene quality or recognize speech.
- Uses 0.6-second dissolves when selected and feasible. It shortens overlaps or switches to cuts when footage is short. Five clips with less than 25 seconds of usable footage cannot render; no frames are invented or clips looped to hit the target.
- Fits the entire frame by default with the brand padding color. Center crop is optional. Keep mode preserves the first audio track from each clip, aligns it to the edit and crossfades where video dissolves; clips without audio use silence. Mute mode omits the audio stream.

## CLI and portable timeline

```sh
mkdir -p output
python -m video_engine autoedit intro.mov detail.mp4 counter.mov team.mp4 closing.mov \
  --transition dissolve --audio keep --fit contain --logo logo.png \
  --background-color '#102A43' --text-color '#F9C74F' --text 'A morning at the studio' \
  -o output/timeline.json
python -m video_engine render output/timeline.json -o output/finished.mp4
```

The CLI prints a JSON validation report. `autoedit` will not overwrite a timeline and `render` will not overwrite an export. Use a local destination filesystem that supports hard links (such as APFS); the renderer publishes a validated temporary output atomically without replacing an existing file. To save on another filesystem, export locally and copy the result there or use the browser download.

[`timeline.schema.json`](../video_engine/timeline.schema.json) defines version `1`: five ordered clips, four joins, brand assets, timed text overlays, an audio policy, and the output canvas. Each `inFrame`/`outFrame` is relative to the start of its source at the 30 fps edit clock; `inFrame` is inclusive and `outFrame` exclusive. A dissolve starts its specified number of `frames` before the end of the accumulated video and reduces the finished frame count by that number. Text `startFrame`/`endFrame` are positions on the finished edit clock. Paths are relative to the timeline's directory unless absolute. Move the JSON and assets together with their relative paths intact, and retain the same renderer build for comparable results. The schema contains no FFmpeg commands or paths to model/services. The renderer also checks duration and whether trim and transition ranges make sense together.

## Tests and limits

```sh
python -m pytest -q -s
```

The tests create synthetic sources at run time and exercise mixed orientations, rotation metadata, VFR, cut and dissolve, kept and muted audio, source file checksums, branding, invalid/HDR rejection, and the browser-facing upload/render/preview/save HTTP flow. They use FFmpeg's `libvpx-vp9` encoder for WebM fixtures. Marked frames check the content of joins independently of the timestamp check. The renderer gates each export on the normalized trim lengths, actual frame timestamps, codec and SDR color profile, aspect ratios, `moov` before `mdat`, and requested audio stream/timing. Black/freeze/duplicate results are **advisories** because intentionally static or dark footage can trip those checks; the renderer cannot tell whether an unmarked real shot is editorially correct.

Input envelope: MOV/MP4/MKV/WebM containing H.264, HEVC, VP8, VP9 or AV1, up to 180 seconds and 60 fps per clip and at most 4K pixel area. Right-angle rotation and non-square pixel aspects are normalized; other rotations and unsupported colors (including tagged HDR) are rejected. Untagged colors are assumed to be limited SDR BT.709 unless the format indicates full range, and the report records that assumption. The 4K bound is an input ceiling, not a performance guarantee. Unusual timestamps may fail validation. There is no HDR tone mapping, loudness normalization, frame-level editor UI, or semantic trimming. Web previews depend on your browser's H.264/AAC support. Real-device playback, Apple Silicon runtime and owner review of real-footage edits still need confirmation.

## Licensing

FFmpeg's default build is LGPL, but the Homebrew build uses GPL `libx264` and is GPL-enabled. This prototype invokes the separately installed executable and does not ship that binary. Review the exact FFmpeg build and GPL/LGPL obligations before bundling or distributing executables or containers; H.264/AAC patent and licensing questions are separate. See [FFmpeg's legal notes](https://ffmpeg.org/legal.html) and the [Homebrew FFmpeg formula](https://formulae.brew.sh/formula/ffmpeg).

The local HTTP interface uses Python's standard library and no web framework. Runtime dependencies are [Pillow](https://pillow.readthedocs.io/en/stable/about.html#license) (HPND) and [jsonschema](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING) (MIT); pytest is test-only (MIT). Titles use the bundled upstream [Noto Sans Regular](https://github.com/notofonts/noto-fonts/tree/main/hinted/ttf/NotoSans) by default under the [SIL Open Font License 1.1](../video_engine/fonts/OFL.txt), with no system font required. It covers accented Latin text and common punctuation; unsupported characters, including most emoji, may appear as placeholders. Supply a font with the needed glyphs for other scripts. Use logo, video, and font assets you have permission to use; a font's license can limit embedding or redistribution even if a rasterized title is allowed.

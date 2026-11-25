# Automated video editing: technical feasibility

**Recommendation: proceed to a local proof of concept only after approval.** A native FFmpeg render produced a 27-second, 1080 × 1920 MP4 from five mixed-orientation fixtures in a median of 9.46 seconds on one CPU-only test machine. This establishes that the media operations can run together, not that the engine can yet choose compelling edits or handle arbitrary phone footage. Use a versioned timeline specification and a replaceable FFmpeg worker; keep the future mobile web UI separate from export.

## What the engine must do

- Take five user-owned original clips, automatically select a range from each, and arrange the ranges into a coherent cut.
- Export a 25–30 second vertical MP4 at exactly 1080 × 1920. Use H.264, 30 fps, square pixels and `yuv420p` as the initial compatibility profile; test playback on phones rather than assuming that the profile is sufficient.
- Apply a configurable logo, palette and typography, and support hard cuts and simple dissolves.
- Handle landscape, portrait and square footage, including files whose displayed orientation comes from metadata. Do not stretch shots or require that users reformat their originals.
- Leave original files intact. The export re-encodes selected source frames and composites only edits and supplied brand assets; no generated scenes, replacement footage, or outpainting.

## Technology comparison

Complexity and performance below are relative to this five-clip workflow. Only native FFmpeg was timed; the other assessments are based on their architecture and published documentation, not comparative measurements.

| Candidate | Implementation complexity | Rendering performance and infrastructure cost | Licensing | Mobile-first web fit |
| --- | --- | --- | --- | --- |
| **Native FFmpeg / libav** ([filters][ff-filters], [licensing][ff-legal]) | Medium using the `ffmpeg` / `ffprobe` CLIs and a small graph compiler; higher if embedding libav directly. No UI framework or timeline model to maintain inside the renderer. | Native, CPU-only baseline measured below. No browser runtime; pay for job CPU, memory, scratch, storage and transfer if hosted later. | Default LGPL 2.1+; enabling GPL components such as `libx264` makes the resulting FFmpeg build GPL (GPL v2+ for the usual build without requirements for a later version). Audit exact build and distribution terms. | Strong for server export and compatible proxies; use ordinary browser playback and lightweight overlays for preview. |
| **Remotion** ([renderer][rem-render], [player][rem-player], [performance][rem-perf], [license FAQ][rem-faq]) | Medium for teams already using React; easier for complex typography and graphics. Still requires source normalization, asset handling and editorial logic. | Server renderer uses headless Chrome and video encoding; frame/compositor work and concurrency can raise CPU and memory needs. Can self-host; commercial licensing may add cost independently of compute. | Source-available, not an unrestricted open-source license. Current free terms cover individuals, qualifying nonprofits, small teams or organizations (up to three people), and non-commercial evaluation. Larger for-profit organizations generally need a Company License for commercial use; assess the whole organization and exact version's terms, not just the number of developers. [Published automator pricing][rem-cost] lists $0.01/render with a $100/month minimum for ineligible users; verify current terms before adopting. | React Player can preview the same composition in-browser. Scrubbing and export parity with several phone clips still need device testing. Consider only if visual templates become substantially richer and license terms fit. |
| **MLT / melt** ([docs][mlt-docs], [source][mlt-source]) | Medium to high. Native editing model, tracks and transitions; adds a timeline API or XML format, plugins and packaging decisions. | Native headless rendering; cost depends on active plugins, scaling and encoder. Same broad worker/storage cost profile as FFmpeg, with a larger stack to package. | LGPL core; audit modules, plugins and bundled codecs independently. | Good alternate backend for a more complex multitrack editor; separate browser preview. |
| **GStreamer Editing Services** ([GES][ges], [licensing][gst-legal]) | High for this scope. Flexible timeline and pipelines, but more plugin, runtime and pipeline integration work. | Native streaming and optional hardware paths depend on plugins and machine; no parity estimate without measuring. Self-hosted worker costs. | GES and GStreamer core are LGPL; plugin and codec obligations vary. | Suitable as a backend, particularly if live capture or streaming becomes central; not a browser editing UI. |
| **Editly / MoviePy** ([Editly][editly], [MoviePy][moviepy]) | Low for a demo; faster authoring via a declarative Node tool or Python scripting. Less direct control over phone-media edge cases and packaging. | FFmpeg still does some processing; Node/graphics or Python frame compositing can add CPU and memory overhead. No hosted service required. | Both MIT at the project level; FFmpeg, fonts and other dependencies retain their own terms. | Good local references; neither gives the mobile browser a preview or removes the need for a backend render. |
| **ffmpeg.wasm / WebCodecs** ([Wasm performance][wasm-perf], [Wasm licensing][wasm-faq], [WebCodecs][webcodecs]) | Wasm can reuse parts of the graph; WebCodecs exposes low-level codec primitives and needs separate demuxing, muxing, compositing and timeline logic. | Wasm moves compute onto the device but copies data into/out of its virtual filesystem; project's benchmarks trail native, and multithreading needs cross-origin isolation. WebCodecs can use device acceleration, but codec support, memory, battery and background behavior vary. | Wasm wrapper is MIT; the compiled FFmpeg core and linked codecs retain their licenses. WebCodecs is a browser API, not a licensed renderer; audit any helper libraries and codecs. | Useful for selective local previews/proxies after feature detection. Do not require phone-side 1080 × 1920 export for the first release. |

No service purchase or deployed infrastructure is needed for a local evaluation. A hosted editing API is not recommended: it adds per-render costs, upload/privacy dependencies and a proprietary timeline contract. If hosting is approved later, first estimate **compute per successful export ≈ billed worker seconds × hourly rate / 3,600**, with separate accounting for failed/retried jobs, queue idle time, proxy generation, retained originals, exports and egress. At an illustrative $0.10 per worker-hour and 9.46 billed seconds, compute alone would be about $0.00026 per render; this is not a price quote or a production estimate. Real phone footage and concurrency can materially change it.

### FFmpeg measurement: one graph, no deployed infrastructure

Ubuntu 22.04.5 LTS (Linux x86-64), Intel Xeon virtual processor with 8 logical CPUs visible, 31 GiB RAM visible, no GPU acceleration. FFmpeg `4.4.2-0ubuntu0.22.04.1` with GPL-enabled `libx264` and FreeType; GNU `time` around each render. Three consecutive renders used the same local inputs, one job at a time.

| Run | Wall time | CPU time (user + system) | Peak RSS | Output |
| --- | ---: | ---: | ---: | --- |
| 1 | 9.50 s | 30.63 s | 488,368 KiB | 27.000 s, 810 frames |
| 2 | 9.46 s | 30.45 s | 488,148 KiB | 27.000 s, 810 frames |
| 3 | 8.81 s | 28.17 s | 487,980 KiB | 27.000 s, 810 frames |

`ffprobe` reported H.264, 1080 × 1920, `yuv420p`, 30 fps and 10,228,935 bytes in all three runs. Median wall time was about 0.35 seconds per second of finished video. The fixture patterns and sample logo were generated **only for this measurement**; the proposed engine does not generate footage.

To reproduce the workload:

1. Make five 7-second `testsrc2` inputs with `ffmpeg -f lavfi -i "testsrc2=s=WIDTHxHEIGHT:r=FPS:d=7" -c:v libx264 -threads 2 -preset ultrafast -crf 20 input.mp4`. Use 1920 × 1080 @ 30, 1080 × 1920 @ 24, 1080 × 1080 @ 25, 1280 × 720 @ 60, and 720 × 1280 @ 30. Supply a 168 × 96 RGBA PNG logo and DejaVu Sans TTF.
2. In a single `-filter_complex`, for each input `i`, map `[i:v]trim=start=0.5:duration=6,setpts=PTS-STARTPTS,fps=30,scale=1080:1920:force_original_aspect_ratio=increase:force_divisible_by=2,crop=1080:1920,setsar=1,format=yuv444p[vi]`. Chain four `xfade=transition=fade:duration=0.75` filters at offsets 5.25, 10.50, 15.75 and 21.00 seconds (relative to the accumulated timeline, quantized to frames by FFmpeg). Then convert to `yuv420p`, `overlay=x=W-w-72:y=112` using the PNG, and `drawtext` with DejaVu Sans, `fontcolor=0xF9C74F`, `fontsize=54`, `box=1:boxcolor=0x102A43@0.7:boxborderw=18`, and `x=72:y=h-th-160`. Use six inputs total, with the logo last.
3. Time the render with `-threads 2` before each video input, `-filter_complex_threads 2`, and map the named final filter output with `-an -c:v libx264 -threads 4 -preset veryfast -crf 22 -movflags +faststart`. Check frame count and format with `ffprobe -count_frames`. Nominal duration: `5 × 6 − 4 × 0.75 = 27` seconds.

The timing includes decode, filters, encode and local file output, not fixture creation, probing, uploads or job scheduling. The graph used center crop, fixed trim windows and no audio. Output structure was checked programmatically; this is not a visual quality, mobile playback, HDR/HEVC, rotation-metadata, concurrency or automatic shot selection test. Do not extrapolate the timings to those workloads.

## Recommended architecture

**Originals → media probe → edit planner → validated timeline → FFmpeg worker → verified MP4.** Run the entire pipeline locally with files and CLI tools. No API, cloud account or external model is required by the design.

1. **Ingest and probe.** Treat the five originals as read-only assets. Use `ffprobe` JSON to record duration, streams, rotation/display matrix, pixel and sample aspect, timestamps, codec and color information. Start with explicitly supported input profiles and return per-clip errors for corrupt, unsupported or over-budget media. Keep media data, derived intermediates and render outputs separate.
2. **Plan a draft deterministically.** Start with usable ranges that avoid black starts/ends and obvious interruptions; use cheap brightness and coarse motion signals as hints, not a claim of semantic understanding. Allocate a balanced share of the target duration across all five clips, redistributing unused frames from short clips. Keep input order as the fallback; where available, use a simple opening/closing signal or manual role to place an opener and closer, leaving the intervening order stable. Save the choices so users can override in/out, order or framing without rescanning media. Calculate target frames as the sum of selected lengths minus overlaps; shorten dissolves or use cuts when handles are too short. If there is insufficient usable footage even with cuts, report it instead of repeating or inventing footage.
3. **Use an engine-neutral, versioned timeline.** Validate asset IDs, source ranges, ordered segments, rational source timestamps and integer output frame positions, fit/focal point, transition type and frame count, audio policy, canvas profile and brand settings. Reference actual logo/font assets and their versions, rather than host font names or FFmpeg expressions. Give jobs a spec version, immutable asset identifiers and renderer build information so renders can be replayed on another machine. Keep ordering/range selection out of the filtergraph compiler.
4. **Normalize and composite in a worker.** Pin a maintained FFmpeg build; normalize orientation exactly once (honor the display matrix, then do not carry it into the export), normalize timestamps, frame rate, time base, aspect and color pipeline before joins. Display the full frame by default using contain with a solid brand-color pad; allow cover with a focal point or optional blurred padding derived from the same source. Use concatenation for cuts and `xfade` between normalized streams for dissolves. Add an alpha PNG logo and basic text with a pinned font via `drawtext`; if complex text shaping is needed, render text-only overlays with a tested layout engine. Validate color, placement and safe margins. If retaining clip audio, trim from the same clock, normalize channel layout and sample rate, insert silence for missing streams and use `acrossfade` for dissolves. Start with a simple explicit audio choice rather than assuming audio can be discarded.
5. **Package and check.** Export MP4 with fast-start metadata, then probe the actual duration, dimensions, codec and frame rate before releasing it. Keep the graph compiler and local worker small; use a process argument list, bounded resource use, and local trusted paths instead of evaluating user-supplied filter strings or URLs. A future job queue/storage adapter can change without changing timeline semantics; nothing in the spec requires a vendor or hosted renderer.
6. **Preview on mobile, render off device.** Use `<video playsinline>` with local compatible clips or low-resolution H.264 proxies as bandwidth allows. Drive trim handles, reorder controls, fit and overlays from the same spec; exact dissolve timing and typography should be checked via a rendered proxy/reference when browser preview differs. Plan for resumable uploads and asynchronous export status when the web app is approved. Treat WebCodecs as a progressive enhancement, not an availability requirement.

## Technical risks and mitigations

| Risk | Mitigation / what to verify |
| --- | --- |
| Technically valid but unconvincing edits | Evaluate on consented real clips with the owner; review openings, abrupt cuts, clipped speech and weak framing. Keep deterministic fallbacks, explain which ranges were picked and support overrides. |
| Portrait metadata, VFR, anamorphic or HDR/HEVC inputs | Probe before editing; normalize rotation, display aspect and timestamps; define and test a deliberate SDR tone-map path or return a clear unsupported-media error. Verify color and time sync against reference clips, not only file metadata. |
| Cropping people, obscuring text or differing preview/export glyphs | Use contain by default, optional focal points and visible safe areas, consistent asset versions, and test alpha, contrast and glyph coverage; do not silently fall back to a different font. |
| A/V drift, audible cuts and mixed/missing tracks | Anchor audio and video to the same timeline; test padding, sample-accurate trimming, crossfades, and whether loudness handling is needed. |
| Performance spikes and large uploads | Cap source duration, size and decoded pixels; bound worker concurrency, scratch and time; benchmark heavier inputs and report upload/proxy latency independently. |
| User footage and untrusted media | Patch and isolate the worker; restrict input paths and network access, bound logs and runtime, and define retention/deletion for originals, proxies and exports before any shared service. |
| License or codec obligations | Before distributing binaries, WASM or container images, review FFmpeg build flags, applicable GPL/LGPL redistribution obligations, any encoder/font/brand-asset licenses and H.264/AAC patent questions separately. A CLI boundary makes the component replaceable; it is not a legal conclusion. |

## Proposed proof of concept (approval required)

1. Assemble a consented local fixture set: five mixed-aspect shots per job, including a rotated phone recording, VFR, mixed/missing audio, off-center subject, short source, and an invalid file. Include a 4K/HEVC or HDR stress case to qualify or explicitly reject it. Establish the supported input envelope before comparing timings.
2. Define the timeline schema and a simple deterministic planner. Test that every viable job uses all five clips, handles insufficient footage with a useful error, stays between 25 and 30 seconds after overlap accounting, and produces the same timeline on repeated runs with identical inputs. Review at least three real five-clip sets with the owner for trim, narrative order and framing; record why edits need changes.
3. Build only a local CLI prototype using a pinned worker. Test cuts, dissolves, contain/cover and configurable logo placement, color and font with two distinct brand presets. Validate using `ffprobe` and frame checks plus direct playback: exact 1080 × 1920, H.264 at 30 fps in a 25–30 second MP4, correct rotation, no stretching, no missing/black frames at joins, and correct overlays. Compare original file checksums before and after, and use timeline ranges to account for exported footage. For retained audio, check sync at the start, joins and end, including silent inputs.
4. Measure wall time, peak memory, output size and failure modes for repeated single jobs and two concurrent jobs on declared hardware. Use a provisional local target of at most 60 seconds and 2 GiB peak RSS per single render on similar 8-logical-CPU hardware for the supported fixture envelope, not as a production SLA. Play the export on iOS Safari and Android Chrome; test a low-resolution preview against the same timeline and note any parity gaps. Compare Remotion only if FFmpeg's branding/template constraints fail the owner's review and the license is viable.

**Open decisions:** Which orientation treatment should users see by default? Should original speech be preserved, muted or mixed? What makes a good opener/closer for the intended clips? Which phone formats and source file limits must the first release accept? What brand safe areas and font rights are required? What export wait time and retention policy are acceptable? Implementation pauses here pending approval.

[ff-filters]: https://ffmpeg.org/ffmpeg-filters.html#xfade
[ff-legal]: https://ffmpeg.org/legal.html
[rem-render]: https://www.remotion.dev/docs/renderer
[rem-player]: https://www.remotion.dev/docs/player/
[rem-perf]: https://www.remotion.dev/docs/performance
[rem-faq]: https://www.remotion.dev/docs/license/faq
[rem-cost]: https://www.remotion.dev/license
[mlt-docs]: https://www.mltframework.org/docs/
[mlt-source]: https://github.com/mltframework/mlt
[ges]: https://gstreamer.freedesktop.org/documentation/gst-editing-services/
[gst-legal]: https://gstreamer.freedesktop.org/documentation/frequently-asked-questions/licensing.html
[editly]: https://github.com/mifi/editly
[moviepy]: https://zulko.github.io/moviepy/
[wasm-perf]: https://ffmpegwasm.netlify.app/docs/performance/
[wasm-faq]: https://ffmpegwasm.netlify.app/docs/faq/
[webcodecs]: https://developer.mozilla.org/en-US/docs/Web/API/WebCodecs_API

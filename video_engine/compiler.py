from .media import NORMALIZE_CLOCK
from .timeline import FPS, layout, lengths

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS
COLOR = "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
SDR_PEAK_NITS = 100


def tone_map(source):
    level = "full" if source.range == "pc" else "limited"
    return (f"zscale=pin=bt2020:tin={source.hdr_transfer}:min=bt2020nc:rin={level}:t=linear:npl={SDR_PEAK_NITS},"
            "format=gbrpf32le,setparams=colorspace=bt2020nc,"
            f"tonemap=mobius:param=0.75:desat=2:peak={source.hdr_peak_nits/SDR_PEAK_NITS:.8g},"
            "zscale=min=gbr:p=bt709:t=bt709:m=bt709:r=limited,format=yuv444p10le,"
            "sidedata=delete:type=MASTERING_DISPLAY_METADATA,sidedata=delete:type=CONTENT_LIGHT_LEVEL,"
            "sidedata=delete:type=DYNAMIC_HDR_PLUS,")


def framing(fit, matrix, level, background):
    if fit == "contain":
        dimensions = ("w='if(gt(dar,9/16),1080,max(2,trunc(1920*dar/2)*2))':"
                      "h='if(gt(dar,9/16),max(2,trunc(1080/dar/2)*2),1920)'")
        return (f"scale={dimensions}:in_color_matrix={matrix}:in_range={level},format=gbrp,"
                f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x{background[1:]},"
                "scale=out_color_matrix=bt709:out_range=tv,format=yuv444p")
    dimensions = ("w='if(gt(dar,9/16),ceil(1920*dar/2)*2,1080)':"
                  "h='if(gt(dar,9/16),1920,ceil(1080/dar/2)*2)'")
    return (f"scale={dimensions}:in_color_matrix={matrix}:in_range={level}:out_color_matrix=bt709:out_range=tv,"
            "crop=1080:1920,format=yuv444p")


def build_graph(spec, sources, overlays):
    graph = []
    keep = spec["audio"] == "keep"
    starts, _ = layout(spec)
    for i, (clip, source) in enumerate(zip(spec["clips"], sources)):
        start, end = clip["inFrame"], clip["outFrame"]
        hdr = tone_map(source) if source.hdr_transfer else ""
        matrix, level = ("bt709", "tv") if hdr else (source.matrix, source.range)
        fit = framing(clip["fit"], matrix, level, spec["branding"]["backgroundColor"])
        graph.append(f"[{i}:{source.video}]{NORMALIZE_CLOCK},"
                     f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,fps={FPS},settb=1/{FPS},"
                     f"{hdr}{fit},setsar=1,{COLOR},showinfo@clip{i}=checksum=0[v{i}]")
        if keep and source.audio is None:
            graph.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=end_sample={(end-start)*SAMPLES_PER_FRAME},"
                         f"aformat=sample_fmts=fltp[a{i}]")
        elif keep:
            graph.append(f"[{i}:{source.audio}]asetpts=PTS-({source.start.numerator}/{source.start.denominator})/TB,"
                         f"aresample={SAMPLE_RATE}:async=1:first_pts=0,"
                         f"aformat=sample_fmts=fltp:channel_layouts=stereo,apad=whole_len={end*SAMPLES_PER_FRAME},"
                         f"atrim=start_sample={start*SAMPLES_PER_FRAME}:end_sample={end*SAMPLES_PER_FRAME},"
                         f"asetpts=PTS-STARTPTS[a{i}]")
    video, audio = "v0", "a0"
    for i, transition in enumerate(spec["transitions"], 1):
        overlap = transition["frames"]
        if overlap:
            graph.append(f"[{video}][v{i}]xfade=transition=fade:duration={overlap/FPS:.9f}:offset={starts[i]/FPS:.9f}[vm{i}]")
            if keep:
                graph.append(f"[{audio}][a{i}]acrossfade=ns={overlap*SAMPLES_PER_FRAME}[am{i}]")
        else:
            single_frame_clock = ",setpts=N" if starts[i] == 1 else ""
            graph.append(f"[{video}][v{i}]concat=n=2:v=1:a=0,settb=1/{FPS}{single_frame_clock}[vm{i}]")
            if keep:
                graph.append(f"[{audio}][a{i}]concat=n=2:v=0:a=1[am{i}]")
        video, audio = f"vm{i}", f"am{i}"
    graph.append(f"[{video}]format=yuv420p[canvas]")
    video = "canvas"
    for i, (_, x, y, enable) in enumerate(overlays):
        graph.append(f"[{5+i}:v:0]scale=out_color_matrix=bt709:out_range=tv,format=yuva420p[mark{i}]")
        graph.append(f"[{video}][mark{i}]overlay=x={x}:y={y}{enable}[canvas{i}]")
        video = f"canvas{i}"
    graph.append(f"[{video}]{COLOR}[out]")
    return ";".join(graph), audio if keep else None

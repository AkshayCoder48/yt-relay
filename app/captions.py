"""Styled ASS caption builder for burned-in Hinglish subtitles.

Cues arrive as [{"start": s, "end": e, "text": str}] in SOURCE-absolute
seconds; clip() offsets them to segment-relative. Style targets Shorts/
Reels UI-safe lower-third placement with big bold text, heavy outline.
"""
from __future__ import annotations

import html


def build_ass(cues: list[dict], seg_start: float, seg_end: float,
              width: int, height: int, font_scale: float = 1.0) -> str:
    fs = round(height * 0.062 * font_scale)
    margin_v = round(height * 0.13)
    margin_lr = round(width * 0.06)
    rows = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Cap,DejaVu Sans,%d,&H00FFFFFF,&H00FFFFFF,&H00101010,"
        "&H8C101010,-1,0,0,0,100,100,0,0,1,%d,%d,2,%d,%d,%d,1" % (
            fs, max(2, fs // 16), max(1, fs // 24), margin_lr, margin_lr,
            margin_v),
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,"
        "Effect,Text",
    ]
    for c in cues:
        a = max(float(c["start"]) - seg_start, 0.0)
        b = min(float(c["end"]) - seg_start, seg_end - seg_start)
        if b <= a:
            continue
        text = html.escape(str(c.get("text", "")).strip())
        if not text:
            continue
        rows.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s" %
                    (_ts(a), _ts(b), text))
    return "\n".join(rows) + "\n"


def _ts(sec: float) -> str:
    sec = max(sec, 0.0)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)

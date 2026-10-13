"""时间轴与字幕。

时间轴是**算出来的，不靠手感**：

    每页镜头时长 = 旁白时长 + 0.5 秒入场 + 0.5 秒出场（不足 6 秒按 6 秒）
    封面镜头固定 6 秒，只有标题和副题，没有旁白

片段是固定 10.04 秒，所以镜头比片段短就裁尾、长就冻结末帧补。
冻结这件事听着糙，但实测运动在 1.5 秒内就收敛了，末段本来就近乎静止，
补出来的部分肉眼看不出来。

**时长必须量化到整帧。** `-t 10.18` 会被 ffmpeg 推到下一帧（10.208），
十几段累积下来整片会比时间轴长 0.2 秒以上，字幕就整体偏晚。
"""
import os

from . import media
from . import project as P

# 字幕样式。描边用接近纯黑的实色（&H00120A04）而不是半透明深棕 ——
# 水彩画面亮部多，半透明描边压不住，浅色页上看不清字。
ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},{ts},&H00FFFFFF,&H00FFFFFF,&H00120A04,&H90000000,-1,0,0,0,100,100,6,0,1,5,3,2,60,60,104,134
Style: SubTitle,{font},{ss},&H00FFF4E2,&H00FFF4E2,&H00120A04,&H90000000,0,0,0,0,100,100,3,0,1,4,3,2,60,60,34,134
Style: Body,{font},{bs},&H00FFFFFF,&H00FFFFFF,&H00120A04,&H90000000,0,0,0,0,100,100,1,0,1,4,3,2,80,80,48,134

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_ts(t):
    """ASS 时间戳 H:MM:SS.cc"""
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)


def wrap_zh(text, per_line=17):
    """中文按字数断行，顺手把跑到行首的收尾标点挪回上一行。"""
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= per_line:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    fixed = []
    for ln in lines:
        if fixed and ln and ln[0] in "。，、！？；：」』）":
            fixed[-1] += ln[0]
            ln = ln[1:]
        if ln:
            fixed.append(ln)
    return fixed


def narration_seconds(cfg, folder, pid):
    """旁白时长。没有旁白返回 0（这一页就只按最短镜头走）。"""
    p = P.find_voice(folder, pid)
    if not p:
        return 0.0
    try:
        return media.dur_of(p, cfg)
    except media.MediaError:
        return 0.0


def build(cfg, folder, data):
    """算时间轴。返回 dict(items=…, total=…, fps=…)。"""
    f = cfg["film"]
    fps = float(cfg["video"]["fps"])
    items = []
    t = 0.0
    for pg in data.get("pages") or []:
        pid = pg["id"]
        kind = pg.get("kind") or "page"
        ndur = 0.0 if kind == "cover" else narration_seconds(cfg, folder, pid)
        if kind == "cover":
            want = float(f["cover_sec"])
        else:
            want = max(float(f["min_shot"]), ndur + float(f["lead"]) + float(f["tail"]))
        # 量化到整帧（见文件头）
        nfr = max(1, int(round(want * fps)))
        shot = nfr / fps
        items.append({
            "id": pid,
            "kind": kind,
            "start": round(t, 3),
            "frames": nfr,
            "shot": round(shot, 4),
            "nar_dur": round(ndur, 3),
            "nar_start": round(t + float(f["lead"]), 3),
            "text": pg.get("text") or "",
            "clip": P.clip_path(folder, pid),
            "voice": P.find_voice(folder, pid),
        })
        t += shot
    return {"items": items, "total": round(t, 3), "fps": fps}


def write_ass(cfg, folder, tl, data, path):
    """按时间轴写字幕文件，返回 (路径, 条数)。"""
    f = cfg["film"]
    head = ASS_HEAD.format(w=cfg["video"]["width"], h=cfg["video"]["height"],
                           font=f["font_name"], ts=f["title_size"],
                           ss=f["subtitle_size"], bs=f["body_size"])
    lines = [head]
    for e in tl["items"]:
        if e["kind"] == "cover":
            title = data.get("title") or ""
            sub = data.get("subtitle") or ""
            if title:
                lines.append("Dialogue: 0,%s,%s,Title,,0,0,0,,%s\n"
                             % (ass_ts(e["start"] + 0.8), ass_ts(e["start"] + e["shot"]),
                                title))
            if sub:
                lines.append("Dialogue: 0,%s,%s,SubTitle,,0,0,0,,%s\n"
                             % (ass_ts(e["start"] + 2.3), ass_ts(e["start"] + e["shot"]),
                                sub))
            continue
        txt = (e["text"] or "").strip()
        if not txt:
            continue
        # 字幕比旁白早 0.15 秒出现、晚 0.25 秒消失，读起来更跟得上
        st = e["nar_start"] - 0.15
        en = e["nar_start"] + max(e["nar_dur"], 1.0) + 0.25
        en = min(en, e["start"] + e["shot"] - 0.05)
        wrapped = "\\N".join(wrap_zh(txt, int(f["wrap"])))
        lines.append("Dialogue: 0,%s,%s,Body,,0,0,0,,%s\n"
                     % (ass_ts(st), ass_ts(en), wrapped))
    with open(path, "w", encoding="utf-8-sig") as fh:
        fh.writelines(lines)
    n = sum(1 for x in lines if x.startswith("Dialogue"))
    return path, n


def describe(cfg, tl, folder):
    """给控制台/界面一份人话版时间轴。"""
    from . import media as M
    rows = []
    for e in tl["items"]:
        n_clip = 1 if os.path.exists(P.clip_path(folder, e["id"])) else 0
        avail = 0.0
        if n_clip:
            try:
                avail = M.dur_of(P.clip_path(folder, e["id"]), cfg)
            except M.MediaError:
                avail = 0.0
        need = e["shot"] - avail
        if not n_clip:
            fit = "缺片段"
        elif need > 0.01:
            fit = "冻结补 %.2fs" % need
        elif need < -0.01:
            fit = "裁掉 %.2fs" % -need
        else:
            fit = "正好"
        rows.append({
            "id": e["id"], "start": e["start"], "shot": e["shot"],
            "frames": e["frames"], "nar_dur": e["nar_dur"],
            "nar_start": e["nar_start"], "voiced": bool(e["voice"]),
            "fit": fit,
        })
    return rows

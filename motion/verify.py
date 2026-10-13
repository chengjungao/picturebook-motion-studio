"""成片出厂校验：一条命令把「容器、画面、音轨、字幕对齐」四件事一起查完。

为什么要专门写这个：成片看上去正常、听上去也正常，但**音轨可能不是从 0 秒
就有实样本**（靠空编辑 elst 表达前导静音）。这种片子在本机所有播放器里都对，
一旦上传到平台被重编码，前导静音就可能丢掉，旁白整体提前。
所以「音轨 start_time 必须为 0」是一条硬指标，得能量出来。

四件事：

1. **容器**：时长 / 帧数 / 音轨 start_time 是否为 0
2. **画面**：分辨率、帧率、像素格式、编码器
3. **音轨**：首次有声时刻、片头是否真的从 0 秒淡入
4. **对齐**：逐条读字幕里的 Body 行，在它标称的旁白起点上取窗量 RMS
"""
import os

from . import media


def check(cfg, film, ass="", frames=5, on_event=None):
    # 回调约定：始终传**一个 dict**，不要用关键字展开。
    # 全项目只有这一种约定，web.py 的 _on_event 也是按这个签名的。
    emit = on_event or (lambda evt: None)
    ff, _fp = media.need_ffmpeg(cfg)
    info = media.probe(film, cfg)
    vid, aud = media.streams(info)
    fmt = info.get("format") or {}
    dur = float(fmt.get("duration") or 0)
    nb = int(vid.get("nb_frames") or 0)
    fps_s = vid.get("r_frame_rate") or "?"
    num, _, den = fps_s.partition("/")
    fps_val = (float(num) / float(den)) if den and float(den) else float(num or 0)

    ok = True
    lines = []
    lines.append("大小      %.1f MB" % (os.path.getsize(film) / 1024.0 / 1024.0))
    lines.append("容器      %.3f s（格式 start_time=%s）"
                 % (dur, fmt.get("start_time", "?")))
    lines.append("画面      %s %sx%s  %s fps  %s  帧数 %s  折算 %.3f s"
                 % (vid.get("codec_name"), vid.get("width"), vid.get("height"),
                    fps_s, vid.get("pix_fmt"), nb,
                    (nb / fps_val) if (nb and fps_val) else 0))
    lines.append("音轨      %s %sHz %sch  时长 %s"
                 % (aud.get("codec_name"), aud.get("sample_rate"),
                    aud.get("channels"), aud.get("duration", "?")))
    st = float(aud.get("start_time") or 0)
    if abs(st) < 1e-6:
        lines.append("音轨起点  start_time=0.000000   ✅ 前段静音是实样本")
    else:
        ok = False
        lines.append("音轨起点  start_time=%.6f   ❌ 非 0，前导静音靠空编辑表达，"
                     "平台重编码可能丢，旁白会整体提前" % st)

    sig = media.decode_mono(film, cfg=cfg)
    lead = int(round(st * 8000))
    if lead > 0:
        # 只解音轨时 ffmpeg 会把输出平移到 0，把 start_time 藏起来。
        # 这里按容器声明补回前段静音，等价于「合规播放器照 elst 走」。
        sig = [0.0] * lead + sig
    t_sound, t_voice, floor = _onset(sig)
    lines.append("")
    lines.append("音轨取样  %.2f s（含补回的前导 %.3f s；静音底取 10 分位 %.5f）"
                 % (len(sig) / 8000.0, lead / 8000.0, floor))
    lines.append("首次有声  %s s   （配乐起点；没放配乐时为空）" % t_sound)
    lines.append("首次人声  %s s   （第 1 页旁白起点）" % t_voice)
    head = "  ".join("%.4f" % media.rms(sig, i * 0.25, (i + 1) * 0.25) for i in range(8))
    lines.append("片头 0~2s（每 0.25s）：%s" % head)
    if any(media.rms(sig, i * 0.25, (i + 1) * 0.25) > 0.002 for i in range(3)):
        lines.append("  ✅ 片头有声，淡入落在实样本上")
    else:
        lines.append("  ⚠️ 片头 0.75 秒内基本无声：要么没放配乐，"
                     "要么淡入落在了空区（amix 吃掉了前段样本）")
    lines.append("片尾最后 1.3 s RMS %.5f   （应为淡出后的低值）"
                 % media.rms(sig, max(0.0, dur - 1.3), dur))

    rows = []
    ass = ass or os.path.join(os.path.dirname(film), "字幕.ass")
    if not os.path.exists(ass):
        alt = os.path.join(os.path.dirname(os.path.dirname(film)), "build", "字幕.ass")
        if os.path.exists(alt):
            ass = alt
    if os.path.exists(ass):
        rows = read_body_lines(ass)
        lines.append("")
        lines.append("旁白定位 vs 字幕标称起点（%d 条）" % len(rows))
        bad = 0
        for i, t in enumerate(rows, 1):
            v_in = media.rms(sig, t - 0.05, t + 0.45)
            v_pre = media.rms(sig, max(0.0, t - 0.75), max(0.0, t - 0.25))
            good = v_in > v_pre * 1.35 and v_in > 0.01
            if not good:
                bad += 1
            lines.append("  %2d  字幕起点 %7.2fs   窗内 %.5f  窗前 %.5f   %s"
                         % (i, t, v_in, v_pre, "✅" if good else "⚠️ 增益不明显"))
        lines.append("  结论：%d/%d 条旁白在字幕标称位置确实有声"
                     % (len(rows) - bad, len(rows)))
        if bad:
            ok = False
    else:
        lines.append("")
        lines.append("！没找到字幕文件（%s），跳过对齐检查" % ass)

    shots = []
    if frames:
        od = os.path.join(os.path.dirname(film), "_出片校验")
        if not os.path.isdir(od):
            os.makedirs(od)
        # 封面标题 0.8 秒才出现、副题 2.3 秒才出现。取 0.5 秒会看到「没有标题」，
        # 那是还没开始，不是没烧上 —— 取样时刻选错会白报一次假故障。
        pick = [("封面标题", 1.6), ("封面副题", 3.2)]
        if rows:
            pick.append(("首页旁白", rows[0] + 0.2))
            pick.append(("末页旁白", rows[-1] + 0.2))
        pick.append(("片尾", max(0.0, dur - 1.0)))
        lines.append("")
        lines.append("抽帧（确认字幕真的烧上去了）")
        for i, (tag, t) in enumerate(pick[:frames]):
            png = os.path.join(od, "成片_%d_%s_t%.1fs.png" % (i, tag, t))
            media.frame_at(film, t, png, cfg)
            shots.append(png)
            lines.append("  %s  t=%.2fs  %s"
                         % (tag, t, os.path.basename(png)))
        lines.append("  目录：%s" % od)

    text = "\n".join(lines)
    for ln in lines:
        emit({"kind": "log", "msg": ln})
    emit({"kind": "verify", "ok": ok, "frames": shots})
    return {"ok": ok, "text": text, "frames": shots}


def _onset(sig, sr=8000, win=0.05):
    """分开报「有配乐贯底」与「人声级」两个起点。

    有配乐时中位窗量到的是配乐而不是静音，拿它当静音底会把阈值抬得过高，
    所以静音底取 10 分位。
    """
    n = int(sr * win)
    vals = []
    for i in range(0, len(sig) - n + 1, n):
        s = 0.0
        for v in sig[i:i + n]:
            s += v * v
        vals.append(((s / n) ** 0.5, i / sr))
    if not vals:
        return None, None, 0.0
    floor = sorted(v for v, _ in vals)[max(0, int(len(vals) * 0.1))]
    thr = max(floor * 2.5, 0.004)
    t_sound = next((ts for v, ts in vals if v > thr), None)
    t_voice = next((ts for v, ts in vals if v > 0.15), None)
    return (None if t_sound is None else round(t_sound, 2),
            None if t_voice is None else round(t_voice, 2), floor)


def read_body_lines(ass):
    """从字幕里读回每页旁白的起点。

    脚本里字幕比旁白早 0.15 秒出现，所以这里要 +0.15 还原成旁白真正的起点。
    """
    rows = []
    with open(ass, encoding="utf-8-sig") as f:
        for line in f:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10 or parts[3].strip() != "Body":
                continue
            st = parts[1].strip()
            h, m, s = st.split(":")
            rows.append(round(int(h) * 3600 + int(m) * 60 + float(s) + 0.15, 3))
    return rows

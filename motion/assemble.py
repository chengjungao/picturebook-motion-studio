"""合成成片：分段整形 → 拼整片 → 混旁白配乐 → 烧字幕 → 淡入淡出。

这一段里有两个坑，改动前务必读：

**坑一：`amix` 会吃掉先到不了的那些样本。**
`amix` 的输出从「第一路输入的起点」开始，其余输入在这一点之前的样本会被
**整段丢弃**（不是补静音）。13 条旁白最早的一路在 6.5 秒，于是配乐的前 6.5 秒
被丢光：封面镜头（0~6 秒）听起来是死寂的，脚本里那 0.8 秒音频淡入也落在空区。
顺带，mp4 音轨起点会被写成 6.4787 秒，靠**空编辑（elst）**表达前导静音 ——
合规播放器照 elst 延迟出声没问题，但平台方重编码一旦丢掉 elst，
旁白就整体提前 6.5 秒。
⇒ 解法是补一条 t=0 的静音流 `[fill]`，并让它当 amix 的**第一路**输入。

**坑二：垫底不能写固定倍率。**
素材本身电平未知，同一倍率可能低到听不见（实测 ACE 出的曲 RMS 只有 -27 dBFS，
乘 0.14 之后是 -44 dBFS，等于没有）。所以先 `loudnorm` 归一到 -16 LUFS，
再整体压 `music_gain_db`，跟素材无关。
"""
import os

from . import media
from . import project as P
from . import timeline as T


def find_music(folder, given=""):
    """配乐是可选件：给路径就用给的，否则看看工程根目录有没有放。"""
    if given:
        return given if os.path.exists(given) else ""
    for n in ("配乐.mp3", "配乐.wav", "配乐.m4a", "music.mp3", "music.wav"):
        p = os.path.join(folder, n)
        if os.path.exists(p):
            return p
    return ""


class Assembler(object):
    def __init__(self, cfg, folder, on_event=None):
        self.cfg = cfg
        self.folder = folder
        self.on_event = on_event or (lambda evt: None)
        self._stop = False

    def cancel(self):
        self._stop = True

    def reset(self):
        self._stop = False

    def _emit(self, **evt):
        self.on_event(evt)

    # ---------- 1. 分段整形 ----------

    def _shape(self, e, segs):
        cfg = self.cfg
        fps = float(cfg["video"]["fps"])
        ff, _fp = media.need_ffmpeg(cfg)
        out = os.path.join(segs, "seg_%s.mp4" % P.norm_id(e["id"]))
        srcs = P.clips_for(self.folder, e["id"])
        if not srcs:
            raise media.MediaError("缺片段：clips/%s.mp4（先跑「出片段」这一步）"
                                   % P.norm_id(e["id"]))
        # 用 concat 滤镜接，不走 -c copy 的 demuxer，接缝处不会闪帧；
        # 再用 tpad 冻结末帧补长，最后按帧数裁齐
        if len(srcs) == 1:
            fin, chain = ["-i", srcs[0]], "[0:v]"
        else:
            fin = []
            for s in srcs:
                fin += ["-i", s]
            chain = ("".join("[%d:v]" % i for i in range(len(srcs)))
                     + "concat=n=%d:v=1:a=0[cat];[cat]" % len(srcs))
        pad = e["shot"] + 0.5
        media.run([ff, "-hide_banner", "-loglevel", "error", "-y"] + fin +
                  ["-filter_complex",
                   chain + "tpad=stop_mode=clone:stop_duration=%.3f[v]" % pad,
                   "-map", "[v]", "-frames:v", str(e["frames"]), "-r", str(int(fps)),
                   "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                   "-pix_fmt", "yuv420p", "-an", out], "整形 %s" % e["id"], cfg)
        return out

    # ---------- 2. 主流程 ----------

    def run(self, data=None, tl=None, music="", subtitles=True, out_name=""):
        cfg = self.cfg
        f = cfg["film"]
        folder = self.folder
        P.ensure_dirs(folder)
        data = data or P.load(folder)
        tl = tl or T.build(cfg, folder, data)
        segs = P.build_dir(folder)
        total = tl["total"]

        title = data.get("title") or "我的绘本"
        out = out_name or os.path.join(P.out_dir(folder), "%s_动画版.mp4" % title)

        # 1. 分段
        self._emit(kind="log", msg="按时间轴整形分段")
        seg_files = []
        for e in tl["items"]:
            if self._stop:
                return {"ok": False, "error": "已取消"}
            s = self._shape(e, segs)
            seg_files.append(s)
            self._emit(kind="seg", id=e["id"],
                       seconds=media.dur_of(s, cfg),
                       kb=round(os.path.getsize(s) / 1024))

        # 2. 拼整片
        self._emit(kind="log", msg="拼成整片（画面）")
        lst = os.path.join(segs, "_all.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for s in seg_files:
                fh.write("file '%s'\n" % s.replace("\\", "/").replace("'", "'\\''"))
        video_only = os.path.join(segs, "_video_only.mp4")
        media.run([media.need_ffmpeg(cfg)[0], "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", video_only],
                  "拼整片", cfg)
        self._emit(kind="log", msg="画面总长 %.2f 秒（时间轴 %.3f 秒）"
                   % (media.dur_of(video_only, cfg), total))

        # 3. 字幕
        ass_path = os.path.join(segs, "字幕.ass")
        _p, n_dlg = T.write_ass(cfg, folder, tl, data, ass_path)
        self._emit(kind="log", msg="字幕 %d 条" % n_dlg)

        # 4. 混音
        ff, _fp = media.need_ffmpeg(cfg)
        cmd_in = ["-i", video_only]
        delays = []
        n = 0
        for e in tl["items"]:
            if e["kind"] == "cover" or e["nar_dur"] <= 0 or not e["voice"]:
                continue
            src = e["voice"]
            if not src.lower().endswith(".wav"):
                # 非 wav 先统一转成 wav，后面滤镜链就只面对一种输入
                w = os.path.join(segs, "_voice_%s.wav" % P.norm_id(e["id"]))
                media.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                           "-i", src, "-ac", "1", "-ar", "48000", w], "转旁白", cfg)
                src = w
            cmd_in += ["-i", src]
            n += 1
            delays.append((n, int(round(e["nar_start"] * 1000))))
        self._emit(kind="log", msg="旁白 %d 条入轨" % n)

        parts = []
        if n:
            # [fill] 必须是第一路（见文件头坑一）
            parts.append("anullsrc=r=48000:cl=mono,atrim=0:%.3f,"
                         "asetpts=PTS-STARTPTS[fill]" % total)
            # 旁白原声 RMS 只有 -25 dBFS 左右，先按 EBU R128 归一到 -16 LUFS 再就位
            parts += ["[%d:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,"
                      "adelay=%d:all=1[a%d]" % (i, d, i) for i, d in delays]
            parts.append("[fill]" + "".join("[a%d]" % i for i, _ in delays)
                         + "amix=inputs=%d:normalize=0:dropout_transition=0[nmix]"
                         % (n + 1))
        else:
            parts.append("anullsrc=r=48000:cl=mono,atrim=0:%.3f,"
                         "asetpts=PTS-STARTPTS[nmix]" % total)
        src_label = "[nmix]"

        music = find_music(folder, music)
        if music:
            cmd_in += ["-i", music]
            parts.append("[%d:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume=%sdB,"
                         "aresample=48000,apad,atrim=0:%.3f[bed]"
                         % (n + 1, f["music_gain_db"], total))
            parts.append("%s[bed]amix=inputs=2:normalize=0:dropout_transition=0[premix]"
                         % src_label)
            src_label = "[premix]"
            self._emit(kind="log", msg="配乐：%s（归一到 -16 LUFS 后压 %d dB 垫底）"
                       % (os.path.basename(music), abs(f["music_gain_db"])))
        else:
            self._emit(kind="log", msg="没放配乐，只出旁白（把 配乐.mp3 放进工程根目录就会自动垫上）")

        parts.append("%sapad,atrim=0:%.3f,alimiter=limit=0.95,"
                     "afade=t=in:st=0:d=%s,"
                     "afade=t=out:st=%.3f:d=%s[aout]"
                     % (src_label, total, f["fade_in"],
                        total - float(f["fade_out"]), f["fade_out"]))
        fc = ";".join(parts)

        # 5. 收尾画面：烧字幕 + 首尾淡入淡出
        #    （就算不要字幕，淡入淡出照做，所以这一步恒跑）
        vf = []
        if subtitles:
            # 滤镜里的路径要转义（见 media.py 文件头坑 2）
            vf.append("subtitles='%s':fontsdir='%s'"
                      % (media.esc_filter_path(ass_path),
                         media.esc_filter_path(f["font_dir"])))
        vf.append("fade=t=in:st=0:d=%s" % f["fade_in"])
        vf.append("fade=t=out:st=%.3f:d=%s" % (total - float(f["fade_out"]), f["fade_out"]))
        final_v = os.path.join(segs, "_final_v.mp4")
        self._emit(kind="log", msg="烧字幕 + 淡入淡出")
        media.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", video_only,
                   "-vf", ",".join(vf),
                   "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                   "-pix_fmt", "yuv420p", final_v], "收尾画面", cfg)

        # 6. 混音并封装
        self._emit(kind="log", msg="混音并封装成片")
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", final_v] + cmd_in[2:]
        cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", out]
        if not os.path.isdir(os.path.dirname(out)):
            os.makedirs(os.path.dirname(out))
        media.run(cmd, "混音封装", cfg)

        size_mb = round(os.path.getsize(out) / 1024.0 / 1024.0, 1)
        self._emit(kind="film", path=out, seconds=media.dur_of(out, cfg),
                   kb=None, mb=size_mb, subtitle_count=n_dlg, ass=ass_path,
                   music=os.path.basename(music) if music else "",
                   narration=n)
        return {"ok": True, "film": out, "seconds": total, "mb": size_mb,
                "ass": ass_path, "narration": n, "subtitles": n_dlg}

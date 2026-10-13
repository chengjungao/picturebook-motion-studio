"""ffmpeg / ffprobe 封装。

这里踩过的四个坑都写在代码旁边了，改之前先读一遍：

1. **`-ss` 必须在 `-i` 之后。** 放前面是输入侧定位，时间戳会归零，
   ASS 字幕**一条都不显示而且不报错**。单张 PNG 还要 `-update 1`。
2. **滤镜里的路径要转义。** `subtitles='D\\:/x/字幕.ass':fontsdir='C\\:/Windows/Fonts'`
   —— 盘符那个冒号不转义，filter 解析会在 `C:` 处断掉，报
   `No option name near '/Windows/Fonts'`。
3. **时长要量化到整帧。** `-t 10.18` 会被推到下一帧（10.208），
   十几段累积下来整片比时间轴长 0.2 秒以上，字幕就整体偏晚。
   用 `-frames:v round(秒 × fps)` 代替 `-t`。
4. **`-vsync` 已被移除。** 需要保持原始帧时序就用 `-fps_mode passthrough`。
"""
import array
import json
import os
import shutil
import subprocess
import sys

from . import config as cfgmod

_WIN_CANDIDATES = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]


class MediaError(Exception):
    """带用户可读原因的 ffmpeg 错误。"""


def _probe_name(ff):
    base = os.path.basename(ff)
    return "ffprobe.exe" if base.lower().endswith(".exe") else "ffprobe"


def _find(kind, override=""):
    """按 配置 → 环境变量 → PATH → 常见安装位置 依次找。"""
    if override and os.path.exists(override):
        return override
    env = os.environ.get(kind.upper())
    if env and os.path.exists(env):
        return env
    found = shutil.which(kind)
    if found:
        return found
    for c in _WIN_CANDIDATES:
        cand = c if kind == "ffmpeg" else os.path.join(os.path.dirname(c), "ffprobe.exe")
        if os.path.exists(cand):
            return cand
    return ""


def tools(cfg=None):
    """返回 (ffmpeg, ffprobe) 的可用路径，找不到的那项是空串。"""
    cfg = cfg or cfgmod.load()
    f = _find("ffmpeg", (cfg.get("ffmpeg") or {}).get("ffmpeg", ""))
    p = (cfg.get("ffmpeg") or {}).get("ffprobe", "")
    if not p and f:
        # 跟 ffmpeg 同目录找 ffprobe，比再猜一遍路径可靠
        guess = os.path.join(os.path.dirname(f), _probe_name(f))
        if os.path.exists(guess):
            p = guess
    if not p:
        p = _find("ffprobe")
    return f, p


def need_ffmpeg(cfg=None):
    f, p = tools(cfg)
    if not f:
        raise MediaError(
            "找不到 ffmpeg。它是本项目唯一必需的外部程序：\n"
            "  Windows：winget install Gyan.FFmpeg   或到 ffmpeg.org 下载后把 bin 加进 PATH\n"
            "  macOS：  brew install ffmpeg\n"
            "  Linux：  sudo apt install ffmpeg\n"
            "装好后重开一次这个窗口即可。")
    if not p:
        raise MediaError("找到了 ffmpeg（%s）但没找到同目录的 ffprobe，请确认是一起装的。" % f)
    return f, p


def run(cmd, label="", cfg=None, quiet=True):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        msg = "「%s」失败\n  cmd: %s\n  stderr: %s" % (
            label or "ffmpeg", " ".join(str(c) for c in cmd)[:500],
            (p.stderr or "").strip()[-1200:])
        raise MediaError(msg)
    if not quiet and p.stdout:
        print(p.stdout[-500:])
    return p


def dur_of(path, cfg=None):
    """读媒体时长（秒）。"""
    ff, fp = need_ffmpeg(cfg)
    p = subprocess.run([fp, "-hide_banner", "-v", "error", "-show_entries",
                        "format=duration", "-of", "json", path],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return round(float(json.loads(p.stdout or "{}")["format"]["duration"]), 3)
    except Exception:
        raise MediaError("读不到时长：%s（文件可能不完整）" % path)


def probe(path, cfg=None):
    """用 JSON 输出，避免 format 段与 stream 段的同名键（duration）互相覆盖。"""
    ff, fp = need_ffmpeg(cfg)
    p = subprocess.run(
        [fp, "-hide_banner", "-v", "error", "-show_entries",
         "format=duration,start_time:stream=index,codec_type,codec_name,"
         "start_time,duration,nb_frames,r_frame_rate,width,height,"
         "pix_fmt,sample_rate,channels",
         "-of", "json", path], capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    try:
        return json.loads(p.stdout or "{}")
    except Exception:
        return {}


def streams(info):
    """从 probe 结果里挑出 (视频流, 音频流)。"""
    ss = info.get("streams") or []
    v = next((s for s in ss if s.get("codec_type") == "video"), {})
    a = next((s for s in ss if s.get("codec_type") == "audio"), {})
    return v, a


def decode_mono(path, sr=8000, cfg=None):
    """解码成 8kHz 单声道浮点样本，用来量电平。

    注意：**只解音轨时 ffmpeg 会把输出平移到 0**，容器声明的 start_time 会被藏起来。
    要按成片时间轴取样，得自己把前导静音补回来（见 verify.py）。
    """
    ff, _fp = need_ffmpeg(cfg)
    p = subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-i", path, "-vn",
         "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True)
    a = array.array("h")
    a.frombytes(p.stdout[:len(p.stdout) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return [x / 32768.0 for x in a]


def rms(sig, t0, t1, sr=8000):
    i = max(0, min(int(t0 * sr), len(sig)))
    j = max(i, min(int(t1 * sr), len(sig)))
    if j <= i:
        return 0.0
    s = 0.0
    for v in sig[i:j]:
        s += v * v
    return (s / (j - i)) ** 0.5


def frame_at(video, t, dst, cfg=None):
    """抽一帧存成图片。`-ss` 放在 `-i` 之后（见文件头坑 1）；已是静态图则加 -update 1。"""
    ff, _fp = need_ffmpeg(cfg)
    run([ff, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "%.3f" % t, "-i", video, "-frames:v", "1", dst], "抽帧", cfg)
    return dst


def scale_to(src, dst, w, h, cfg=None):
    """把页图统一成出片尺寸（不装 Pillow，缩图这活交给 ffmpeg）。"""
    ff, _fp = need_ffmpeg(cfg)
    run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,"
                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (w, h, w, h),
         "-frames:v", "1", "-update", "1", dst], "缩放页图", cfg)
    return dst


def esc_filter_path(path):
    """滤镜里的 Windows 路径：反斜杠换正斜杠，盘符冒号要转义（见文件头坑 2）。"""
    return path.replace("\\", "/").replace(":", "\\:")

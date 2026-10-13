"""旁白。

本项目**不替你念**，只做两件事：

1. **收音频**。你自己录、或用任意朗读工具生成，丢进界面，它会统一转成
   24kHz 单声道 wav，排好名字放进 voice/。
2. **（可选）调本地 Kokoro**。如果你已经在电脑上装好了 Kokoro-82M 的中文版，
   可以在 config.json 里填上它的模型目录和 Python 解释器路径，界面就会出现
   「一键生成旁白」。Kokoro 是纯 CPU 的，不占显存。

为什么不内置 TTS：这会引入 torch 这一串重依赖，把「解压就能用」变成
「先装半小时环境」。念稿这件事，豆包、系统朗读、录音笔都能干，没必要绑死。
"""
import os
import subprocess
import sys

from . import config as cfgmod
from . import media
from . import project as P

# 交给独立解释器跑的小脚本。Kokoro 的依赖装在它自己的环境里，本项目不碰。
KOKORO_JOB = '''# -*- coding: utf-8 -*-
"""由「让绘本动起来」生成的旁白合成脚本。"""
import json, os, sys

args = json.load(open(sys.argv[1], encoding="utf-8"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import numpy as np
import soundfile as sf
import torch
from kokoro import KModel, KPipeline

md = args["model_dir"]
km = KModel(config=os.path.join(md, "config.json"),
            model=os.path.join(md, "kokoro-v1_1-zh.pth")).eval()
pipe = KPipeline(lang_code="z", repo_id=args["repo"], model=km, device="cpu")
pack = torch.load(os.path.join(md, "voices", args["voice"] + ".pt"),
                  map_location="cpu", weights_only=True)

out = args["out_dir"]
if not os.path.isdir(out):
    os.makedirs(out)
rows = []
for r in args["rows"]:
    txt = (r.get("text") or "").strip()
    dst = os.path.join(out, r["name"] + ".wav")
    if not txt:
        rows.append({"id": r["id"], "dur": 0.0, "wav": None})
        print("SKIP\\t%s" % r["id"])
        continue
    chunks = []
    for _gs, _ps, audio in pipe(txt, voice=pack, speed=args["speed"]):
        if audio is not None:
            chunks.append(audio.detach().cpu().numpy())
    a = np.concatenate(chunks) if chunks else np.zeros(1, dtype="float32")
    sf.write(dst, a, 24000)
    rows.append({"id": r["id"], "dur": round(len(a) / 24000.0, 3), "wav": dst})
    print("OK\\t%s\\t%.2f" % (r["id"], len(a) / 24000.0))

json.dump(rows, open(os.path.join(out, "_kokoro结果.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("DONE")
'''


def _python_ok(path):
    if not path or not os.path.exists(path):
        return False
    try:
        p = subprocess.run([path, "-c", "import kokoro, soundfile, numpy, torch"],
                           capture_output=True, text=True, timeout=120)
        return p.returncode == 0
    except Exception:
        return False


def kokoro_ready(cfg):
    t = cfg.get("tts") or {}
    py = t.get("kokoro_python") or ""
    md = t.get("kokoro_model_dir") or ""
    if not py or not md:
        return False, "还没填 Kokoro 的解释器路径与模型目录（config.json 的 tts 一节）"
    if not os.path.isdir(md):
        return False, "模型目录不存在：%s" % md
    for f in ("config.json", "kokoro-v1_1-zh.pth"):
        if not os.path.exists(os.path.join(md, f)):
            return False, "模型目录里缺 %s" % f
    if not _python_ok(py):
        return False, ("这个解释器跑不起来 Kokoro：%s（缺 kokoro / soundfile / "
                       "numpy / torch 中的一个）" % py)
    return True, ""


def synth_kokoro(cfg, folder, data, on_event=None):
    """调本地 Kokoro 逐页生成旁白。只处理有文字的页。"""
    # 回调约定：始终传一个 dict（见 verify.check 处的说明）
    emit = on_event or (lambda evt: None)
    ok, why = kokoro_ready(cfg)
    if not ok:
        emit({"kind": "error", "msg": why})
        return {"ok": False, "error": why}

    t = cfg["tts"]
    rows = []
    for pg in data.get("pages") or []:
        txt = (pg.get("text") or "").strip()
        if not txt:
            continue
        rows.append({"id": pg["id"], "name": P.norm_id(pg["id"]), "text": txt})
    if not rows:
        emit(kind="error", msg="工程里一页文字都没有，先把绘本正文填上")
        return {"ok": False, "error": "无正文"}

    bd = P.build_dir(folder)
    job = os.path.join(bd, "_kokoro_job.py")
    args = os.path.join(bd, "_kokoro_args.json")
    with open(job, "w", encoding="utf-8") as f:
        f.write(KOKORO_JOB)
    cfgmod.write_json(args, {
        "model_dir": t["kokoro_model_dir"],
        "repo": os.path.basename(t["kokoro_model_dir"].rstrip("/\\")) or "kokoro",
        "voice": t.get("voice") or "zf_001",
        "speed": float(t.get("speed") or 0.9),
        "out_dir": P.voice_dir(folder),
        "rows": rows,
    })

    emit({"kind": "log", "msg": "用本地 Kokoro 生成 %d 条旁白（纯 CPU，不占显存）" % len(rows)})
    p = subprocess.Popen([t["kokoro_python"], job, args],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    total = 0.0
    for line in p.stdout:
        line = line.strip()
        if line.startswith("OK\t"):
            parts = line.split("\t")
            dur = float(parts[2]) if len(parts) > 2 else 0.0
            total += dur
            emit({"kind": "voice", "id": parts[1], "seconds": round(dur, 2)})
        elif line.startswith("SKIP\t"):
            emit({"kind": "voice", "id": line.split("\t")[1], "seconds": 0.0})
        elif line and not line.startswith(("DONE", "OK", "SKIP")):
            emit({"kind": "log", "msg": "  " + line[-200:]})
    p.wait()
    if p.returncode != 0:
        emit({"kind": "error", "msg": "Kokoro 退出码 %d，看看上面的报错" % p.returncode})
        return {"ok": False, "error": "Kokoro 失败"}
    emit({"kind": "log", "msg": "旁白合计 %.1f 秒" % total})
    return {"ok": True, "seconds": round(total, 1), "count": len(rows)}


def import_audio(cfg, folder, pid, filename, blob, on_event=None):
    """收一条旁白：统一转成 24kHz 单声道 wav，按页 id 命名放好。"""
    emit = on_event or (lambda evt: None)
    ff, _fp = media.need_ffmpeg(cfg)
    P.ensure_dirs(folder)
    ext = os.path.splitext(filename or "")[1].lower() or ".wav"
    tmp = os.path.join(P.build_dir(folder), "_upload%s" % ext)
    with open(tmp, "wb") as f:
        f.write(blob)
    dst = P.voice_path(folder, pid)
    media.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", tmp,
               "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", dst], "收旁白", cfg)
    os.remove(tmp)
    sec = media.dur_of(dst, cfg)
    emit({"kind": "voice", "id": pid, "seconds": sec, "imported": True})
    return {"ok": True, "seconds": sec, "path": dst}


def clear_voice(folder, pid):
    p = P.find_voice(folder, pid)
    if p and os.path.basename(p).startswith(P.norm_id(pid)):
        os.remove(p)
        return True
    return False


def total_seconds(cfg, folder, data):
    s = 0.0
    for pg in data.get("pages") or []:
        p = P.find_voice(folder, pg["id"])
        if not p:
            continue
        try:
            s += media.dur_of(p, cfg)
        except media.MediaError:
            pass
    return round(s, 2)


def main_hint():
    """给界面用的一句人话提示。"""
    return "旁白可以自己录，也可以用别处的朗读工具生成，丢进来就行。" \
           "（本项目不内置语音合成，免得为了念几句话先装半小时环境。）"


if __name__ == "__main__":
    sys.exit("这是库文件，请从 app.py 启动界面。")

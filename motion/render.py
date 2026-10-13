"""逐页出片段。

一页一段，整段 10.04 秒。为什么不是「两段 5 秒拼起来」：
 ① 第二段要拿第一段的末帧当首帧，而末帧已经被重编码过一次，接缝处会跳
 ② 漂移会叠加：第二段是从已经漂过一点的画面继续往下走
 
实测对比过，一页一段的画面在 1.5 秒左右就稳定下来，之后只有树叶、水波、
萤火虫这类微动，所以镜头拉到 10 秒也不空。

断点续跑：片段落盘后写 clips/_进度.json，重跑时默认跳过已完成的。
"""
import os
import time

from . import comfy
from . import config as cfgmod
from . import project as P
from . import workflow

STATE_NAME = "_进度.json"


def _state_path(folder):
    return os.path.join(P.clips_dir(folder), STATE_NAME)


def load_state(folder):
    return cfgmod.read_json(_state_path(folder), {}) or {}


def save_state(folder, state):
    cfgmod.write_json(_state_path(folder), state)


class Renderer(object):
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

    def run(self, ids=None, resume=True, limit=0):
        """出片段。ids 为空表示全部页。返回一份汇总。"""
        cfg = self.cfg
        v = cfg["video"]
        P.ensure_dirs(self.folder)
        data = P.load(self.folder)
        pages = [p for p in data["pages"] if not ids or p["id"] in ids]
        if limit:
            pages = pages[:limit]

        per = workflow.estimate_seconds(cfg)
        c = comfy.Comfy(cfg["comfy_url"], timeout=60)
        rep = c.health()
        if not rep["ok"]:
            self._emit(kind="error", msg="ComfyUI 连不上：" + "；".join(rep["errors"]))
            return {"ok": False, "error": "ComfyUI 连不上"}

        state = load_state(self.folder) if resume else {}
        self._emit(kind="start", total=len(pages), per=per,
                   model=cfg["models"]["unet"],
                   size="%d×%d" % (v["width"], v["height"]),
                   frames=v["frames"], fps=v["fps"], steps=v["steps"])
        self._emit(kind="log", msg="模型 %s｜%d×%d｜%d 帧 / %d 步 / cfg %s｜单段约 %.0f 秒，%d 页合计约 %.0f 分钟"
                   % (cfg["models"]["unet"], v["width"], v["height"], v["frames"],
                      v["steps"], v["cfg"], per, len(pages), per * len(pages) / 60))

        done = 0
        t_all = time.time()
        for i, pg in enumerate(pages, 1):
            if self._stop:
                self._emit(kind="log", msg="已取消")
                break
            pid = pg["id"]
            if resume and state.get(pid, {}).get("ok") and os.path.exists(P.clip_path(self.folder, pid)):
                done += 1
                self._emit(kind="skip", index=i, total=len(pages), id=pid)
                continue

            img = P.page_image(self.folder, pid)
            if not img:
                self._emit(kind="error", id=pid,
                           msg="缺页图 img/%s.png（用绘本工坊出好页图再回来）" % pid)
                state[pid] = {"ok": False, "error": "no image"}
                save_state(self.folder, state)
                continue

            self._emit(kind="clip_start", index=i, total=len(pages), id=pid,
                       image=os.path.basename(img))
            try:
                fit = workflow.prep_image(self.folder, pid, img, cfg)
                up = c.upload_image(fit, "motion_%s.png" % P.norm_id(pid))
                wf = workflow.build(cfg, up, workflow.positive_text(pg.get("motion")),
                                    v["base_seed"] + i, "motion_%s" % P.norm_id(pid))
                t0 = time.time()
                pid_task = c.submit(wf)

                tick_state = {"n": 0}

                def tick(elapsed, running, pending, used, peak, _pid=pid,
                         _i=i, _n=len(pages), _t0=t0, _st=tick_state):
                    if int(elapsed) // 20 == _st["n"]:
                        return
                    _st["n"] = int(elapsed) // 20
                    self._emit(kind="clip_tick", id=_pid, index=_i, total=_n,
                               elapsed=round(elapsed, 1),
                               vram=None if used is None else round(used, 2),
                               peak=round(peak, 2))

                ok, files, err = c.wait(pid_task, on_tick=tick,
                                        should_stop=lambda: self._stop)
                if not ok or not files:
                    raise comfy.ComfyError(err or "没有产物")
                blob = c.fetch(files[0])
                dst = P.clip_path(self.folder, pid)
                with open(dst, "wb") as f:
                    f.write(blob)
                el = round(time.time() - t0, 1)
                done += 1
                state[pid] = {"ok": True, "seconds": el,
                              "kb": round(len(blob) / 1024)}
                self._emit(kind="clip_done", id=pid, index=i, total=len(pages),
                           seconds=el, kb=round(len(blob) / 1024))
            except Exception as e:
                state[pid] = {"ok": False, "error": str(e)[:400]}
                self._emit(kind="error", id=pid, msg=str(e)[:400])
            save_state(self.folder, state)

        if self._stop:
            save_state(self.folder, state)
            return {"ok": False, "error": "已取消", "done": done}

        total_min = (time.time() - t_all) / 60
        secs = [x["seconds"] for x in state.values()
                if isinstance(x, dict) and x.get("ok") and x.get("seconds")]
        self._emit(kind="done", done=done, total=len(pages),
                   minutes=round(total_min, 1),
                   median=None if not secs else round(sorted(secs)[len(secs) // 2], 1))
        return {"ok": True, "done": done, "total": len(pages),
                "minutes": round(total_min, 1)}

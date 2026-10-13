"""本地界面。只用标准库的 http.server。

几个刻意的设计：

* **一次只跑一件事。** 出片段要占满显存，合成要占满 CPU，两个一起跑只会互相拖。
  所以后台只留一个任务位，重复点会说「正在忙」。
* **日志用轮询不用推送。** `GET /api/log?since=N` 拿新增的几行，前端 600ms 问一次。
  SSE 更漂亮，但多一段要调的协议，出错还不好查；日志量本来就不大。
* **成片直接给浏览器播**，支持 Range 请求，这样能拖动进度条。
"""
import base64
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import assemble as A
from . import comfy
from . import config as cfgmod
from . import media
from . import project as P
from . import render as R
from . import timeline as T
from . import tts
from . import verify as V
from . import workflow

WEB_DIR = os.path.join(cfgmod.ROOT, "web")
MAX_LOG = 800
APP = None          # 由 run() 填，Handler 从这里拿


class State(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.log = []
        self.seq = 0
        self.busy = False
        self.job = ""
        self.progress = {}
        self.last_verify = None

    def add(self, text, level="info"):
        with self.lock:
            self.seq += 1
            self.log.append({"n": self.seq, "t": time.strftime("%H:%M:%S"),
                             "text": text, "level": level})
            if len(self.log) > MAX_LOG:
                self.log = self.log[-MAX_LOG:]

    def since(self, n):
        with self.lock:
            return [x for x in self.log if x["n"] > n], self.seq

    def reset_log(self):
        with self.lock:
            self.log = []
            self.seq = 0


class App(object):
    def __init__(self, cfg, folder):
        self.cfg = cfg
        self.folder = folder
        self.st = State()
        self._running = None
        P.ensure_dirs(folder)
        if not P.load(folder, auto_create=False):
            P.save(folder, P.from_book(folder))

    # ---------- 事件出口 ----------

    def _on_event(self, evt):
        k = evt.get("kind", "")
        if k == "log":
            self.st.add(evt.get("msg", ""))
        elif k == "start":
            self.st.progress = {"phase": "clips", "index": 0, "total": evt["total"]}
            self.st.add("开始出片段：共 %d 页，单段约 %.0f 秒" % (evt["total"], evt["per"]))
        elif k == "clip_start":
            self.st.progress = {"phase": "clips", "index": evt["index"],
                                "total": evt["total"], "id": evt["id"]}
            self.st.add("[%d/%d] %s 出片中…" % (evt["index"], evt["total"], evt["id"]))
        elif k == "clip_tick":
            self.st.progress = {"phase": "clips", "index": evt["index"],
                                "total": evt["total"], "id": evt["id"],
                                "elapsed": evt.get("elapsed"),
                                "vram": evt.get("vram")}
            self.st.add("  +%.0f 秒　显存 %s GB" % (evt.get("elapsed") or 0,
                                                evt.get("vram")), "dim")
        elif k == "clip_done":
            self.st.add("[%d/%d] %s ✅ %.0f 秒 / %d KB"
                        % (evt["index"], evt["total"], evt["id"],
                           evt["seconds"], evt["kb"]), "good")
        elif k == "skip":
            self.st.add("[%d/%d] %s 已有片段，跳过"
                        % (evt["index"], evt["total"], evt["id"]), "dim")
        elif k == "seg":
            self.st.add("  分段 %s  %.2f 秒" % (evt["id"], evt["seconds"]), "dim")
        elif k == "voice":
            self.st.add("  旁白 %s  %.2f 秒%s" % (evt["id"], evt["seconds"],
                                             "（刚收下）" if evt.get("imported") else ""),
                        "good" if evt.get("imported") else "dim")
        elif k == "film":
            self.st.add("成片：%s" % evt["path"], "good")
            self.st.add("  时长 %.2f 秒　%.1f MB　旁白 %d 条　字幕 %d 条"
                        % (evt["seconds"], evt["mb"], evt["narration"],
                           evt["subtitle_count"]), "good")
        elif k == "verify":
            self.st.last_verify = {"ok": evt.get("ok"),
                                   "frames": [os.path.basename(x)
                                              for x in (evt.get("frames") or [])]}
        elif k == "error":
            self.st.add("✗ %s%s" % (evt.get("id", ""), evt.get("msg", "")), "bad")

    # ---------- 后台任务 ----------

    def start_job(self, fn, name):
        with self.st.lock:
            if self.st.busy:
                return {"ok": False, "error": "正在忙（%s），等这次跑完再点" % self.st.job}
            self.st.busy = True
            self.st.job = name
        self.st.reset_log()
        self.st.add("▶ %s" % name)

        def worker():
            try:
                fn()
            except Exception as e:
                self.st.add("✗ %s" % e, "bad")
            finally:
                with self.st.lock:
                    self.st.busy = False
                    self.st.job = ""
                    self.st.progress = {}
                    self._running = None

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job": name}

    def cancel(self):
        r = self._running
        if r is not None:
            r.cancel()
        self.st.add("已请求停止，当前这一步跑完会停下来", "warn")
        return {"ok": True}

    # ---------- 视图数据 ----------

    def films(self):
        d = P.out_dir(self.folder)
        if not os.path.isdir(d):
            return []
        out = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".mp4")]
        return sorted(out, key=os.path.getmtime, reverse=True)

    def state_payload(self):
        data = P.load(self.folder)
        tl = T.build(self.cfg, self.folder, data)
        t = self.cfg.get("tts") or {}
        kk = tts.kokoro_ready(self.cfg)[0] if t.get("kokoro_python") else False
        return {
            "ok": True,
            "folder": os.path.abspath(self.folder),
            "title": data.get("title") or "",
            "subtitle": data.get("subtitle") or "",
            "pages": P.summary(self.folder, data),
            "timeline": T.describe(self.cfg, tl, self.folder),
            "total": tl["total"],
            "config": self.cfg,
            "busy": self.st.busy,
            "job": self.st.job,
            "films": [{"name": os.path.basename(x),
                       "mb": round(os.path.getsize(x) / 1024.0 / 1024.0, 1)}
                      for x in self.films()],
            "kokoro": kk,
            "voice_hint": tts.main_hint(),
            "verify": self.st.last_verify,
        }

    # ---------- 动作 ----------

    def save(self, body):
        data = P.load(self.folder)
        if isinstance(body.get("title"), str):
            data["title"] = body["title"]
        if isinstance(body.get("subtitle"), str):
            data["subtitle"] = body["subtitle"]
        if isinstance(body.get("pages"), list):
            old = {p["id"]: p for p in data.get("pages") or []}
            new = []
            for pg in body["pages"]:
                pid = pg.get("id")
                if not pid:
                    continue
                base = old.get(pid) or {"id": pid, "kind": "page"}
                base["text"] = (pg.get("text") or "").strip()
                base["motion"] = (pg.get("motion") or "").strip()
                new.append(base)
            if new:
                data["pages"] = new
        P.save(self.folder, data)
        self.st.add("已保存 %s" % os.path.basename(P.manifest_path(self.folder)))
        return {"ok": True}

    def save_config(self, body):
        self.cfg = cfgmod.save_and_reload(body or {})
        self.st.add("配置已保存")
        return {"ok": True, "config": self.cfg}

    def check(self):
        self.st.reset_log()
        cfg = self.cfg
        st = self.st
        st.add("=" * 52)
        st.add("环境自检")
        st.add("=" * 52)
        st.add("Python      %s" % sys.version.split()[0])
        st.add("配置文件    %s" % cfgmod.CONFIG_PATH)
        st.add("工程目录    %s" % os.path.abspath(self.folder))
        ok = True

        ff, fp = media.tools(cfg)
        if ff and fp:
            st.add("ffmpeg      %s" % ff, "good")
        else:
            ok = False
            st.add("ffmpeg      没找到。它是唯一必需的外部程序，先装上。", "bad")

        c = comfy.Comfy(cfg["comfy_url"], timeout=20)
        rep = c.health()
        if not rep["ok"]:
            ok = False
            for e in rep["errors"]:
                st.add("ComfyUI     %s" % e, "bad")
        else:
            i = rep["info"]
            st.add("ComfyUI     %s　版本 %s / Python %s"
                   % (cfg["comfy_url"], i.get("comfyui_version"), i.get("python")),
                   "good")
            if i.get("device"):
                st.add("显卡        %s　显存 %.1f GB（空闲 %.1f GB）"
                       % (i["device"], i.get("vram_total_gb", 0),
                          i.get("vram_free_gb", 0)))
            for w in rep["warnings"]:
                st.add("! %s" % w, "warn")
            try:
                m = c.list_model_choices()
            except comfy.ComfyError as e:
                m = {}
                st.add("读模型清单失败：%s" % e, "bad")
            miss = [n for n in workflow.REQUIRED_NODES
                    if n not in (m.get("nodes") or [])]
            if miss:
                ok = False
                st.add("缺节点      %s（ComfyUI 版本偏旧，升级一下）"
                       % "、".join(miss), "bad")
            else:
                st.add("节点        %d 个必需的都在" % len(workflow.REQUIRED_NODES),
                       "good")
            for k, label in (("unet", "扩散模型"), ("clip", "文本编码器"), ("vae", "VAE")):
                want = cfg["models"][k]
                avail = m.get(k) or []
                if want in avail:
                    st.add("%-10s  %s" % (label, want), "good")
                else:
                    ok = False
                    st.add("%s  里没有 %s" % (label, want), "bad")
                    for x in avail[:6]:
                        st.add("      可选：%s" % x, "dim")

        data = P.load(self.folder)
        n_img = sum(1 for p in data["pages"] if P.page_image(self.folder, p["id"]))
        st.add("页图        %d/%d 页有图" % (n_img, len(data["pages"])),
               "good" if n_img == len(data["pages"]) else "warn")
        gaps = P.motion_gaps(data)
        if gaps:
            st.add("运动描述    %d 页还没填（出片时会套一句兜底描述）" % len(gaps), "warn")
        st.add("=" * 52)
        st.add("结论：%s" % ("环境就绪。" if ok else "还差东西，按上面的 ✗ 补齐再点一次自检。"),
               "good" if ok else "bad")
        return {"ok": ok}

    def plan(self):
        data = P.load(self.folder)
        tl = T.build(self.cfg, self.folder, data)
        rows = T.describe(self.cfg, tl, self.folder)
        self.st.reset_log()
        self.st.add("时间轴：共 %.2f 秒（%.2f 分钟），%d 个镜头"
                    % (tl["total"], tl["total"] / 60.0, len(rows)))
        for r in rows:
            self.st.add("  %-18s 起点 %6.2f　镜头 %5.2f（%3d 帧）　旁白 %5.2f@%6.2f　%s%s"
                        % (r["id"], r["start"], r["shot"], r["frames"], r["nar_dur"],
                           r["nar_start"], "有声 " if r["voiced"] else "无声 ",
                           r["fit"]), "dim")
        return {"ok": True, "rows": rows, "total": tl["total"]}

    def start_clips(self, body):
        ids = body.get("ids") or None
        resume = body.get("resume", True)
        limit = int(body.get("limit") or 0)

        def job():
            r = R.Renderer(self.cfg, self.folder, self._on_event)
            self._running = r
            r.run(ids=ids, resume=resume, limit=limit)

        return self.start_job(job, "出片段")

    def start_kokoro(self):
        def job():
            tts.synth_kokoro(self.cfg, self.folder, P.load(self.folder), self._on_event)

        return self.start_job(job, "生成旁白（本地 Kokoro）")

    def upload_voice(self, body):
        pid = body.get("id") or ""
        blob = body.get("data") or ""
        if not pid or not blob:
            return {"ok": False, "error": "缺页 id 或音频内容"}
        try:
            raw = base64.b64decode(blob.split(",")[-1])
        except Exception:
            return {"ok": False, "error": "音频内容解不开"}
        self.st.reset_log()
        try:
            return tts.import_audio(self.cfg, self.folder, pid,
                                    body.get("name") or "voice.wav", raw,
                                    self._on_event)
        except media.MediaError as e:
            self.st.add("✗ %s" % e, "bad")
            return {"ok": False, "error": str(e)}

    def clear_voice(self, body):
        pid = body.get("id") or ""
        ok = tts.clear_voice(self.folder, pid)
        self.st.add("已清掉 %s 的旁白" % pid if ok else "没有找到 %s 的旁白" % pid)
        return {"ok": ok}

    def fill_motion(self):
        data = P.load(self.folder)
        n = P.fill_missing_motion(data)
        P.save(self.folder, data)
        self.st.add("给 %d 页填上兜底的运动描述，逐页改成更贴切的会好看很多" % n)
        return {"ok": True, "filled": n}

    def start_assemble(self, body):
        music = body.get("music") or ""
        subs = bool(body.get("subtitles", True))

        def job():
            a = A.Assembler(self.cfg, self.folder, self._on_event)
            self._running = a
            a.run(music=music, subtitles=subs)

        return self.start_job(job, "合成成片")

    def verify(self):
        films = self.films()
        if not films:
            return {"ok": False, "error": "还没有成片，先合成一次"}
        self.st.reset_log()
        return V.check(self.cfg, films[0], on_event=self._on_event)

    def reveal(self):
        d = os.path.abspath(self.folder)
        if sys.platform.startswith("win"):
            os.startfile(d)          # noqa
        elif sys.platform == "darwin":
            os.system("open '%s'" % d)
        else:
            os.system("xdg-open '%s'" % d)
        return {"ok": True, "folder": d}


class Handler(BaseHTTPRequestHandler):
    server_version = "MotionStudio"

    def log_message(self, fmt, *args):
        pass            # 静音访问日志，控制台只留我们自己的输出

    # ---------- 基础 ----------

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _file(self, path, ctype=None):
        if not path or not os.path.exists(path):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        if ctype is None:
            ext = os.path.splitext(path)[1].lower()
            ctype = {".html": "text/html; charset=utf-8",
                     ".js": "application/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8",
                     ".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".webp": "image/webp",
                     ".mp4": "video/mp4", ".wav": "audio/wav",
                     ".mp3": "audio/mpeg", ".json": "application/json"}.get(
                ext, "application/octet-stream")
        with open(path, "rb") as f:
            return self._send(200, f.read(), ctype)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _range_file(self, path):
        """成片要能拖进度条，所以支持 Range。"""
        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if not m:
            with open(path, "rb") as f:
                return self._send(200, f.read(), "video/mp4")
        s = int(m.group(1)) if m.group(1) else 0
        e = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        with open(path, "rb") as f:
            f.seek(s)
            body = f.read(max(0, e - s + 1))
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Range", "bytes %d-%d/%d" % (s, e, size))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    # ---------- 路由 ----------

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        a = APP
        try:
            if p in ("/", "/index.html"):
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if p.startswith("/static/"):
                return self._file(os.path.join(WEB_DIR, os.path.basename(p)))
            if p == "/api/state":
                return self._json(a.state_payload())
            if p == "/api/log":
                since = int((parse_qs(u.query).get("since") or ["0"])[0])
                rows, seq = a.st.since(since)
                return self._json({"ok": True, "lines": rows, "seq": seq,
                                   "busy": a.st.busy, "job": a.st.job,
                                   "progress": a.st.progress})
            if p.startswith("/img/"):
                return self._file(P.page_image(a.folder, p[5:]))
            if p.startswith("/镜头校验/"):
                # 校验时抽出来的画面，直接给界面看字幕有没有烧上
                name = os.path.basename(p[len("/镜头校验/"):])
                return self._file(os.path.join(P.out_dir(a.folder), "_出片校验", name))
            if p == "/film":
                films = a.films()
                return self._range_file(films[0]) if films else \
                    self._send(404, "还没有成片", "text/plain; charset=utf-8")
            if p.startswith("/voice/"):
                return self._file(P.find_voice(a.folder, p[7:]))
            return self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        a = APP
        b = self._body()
        routes = {
            "/api/save": lambda: a.save(b),
            "/api/config": lambda: a.save_config(b),
            "/api/check": a.check,
            "/api/plan": a.plan,
            "/api/clips": lambda: a.start_clips(b),
            "/api/voice/kokoro": a.start_kokoro,
            "/api/voice/upload": lambda: a.upload_voice(b),
            "/api/voice/clear": lambda: a.clear_voice(b),
            "/api/assemble": lambda: a.start_assemble(b),
            "/api/verify": a.verify,
            "/api/cancel": a.cancel,
            "/api/reveal": a.reveal,
            "/api/fill_motion": a.fill_motion,
        }
        fn = routes.get(p)
        if not fn:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        try:
            return self._json(fn())
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)


def _find_free_port(preferred=8770):
    for p in [preferred] + list(range(8771, 8790)):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(cfg, folder, port=None, open_browser=True):
    global APP
    APP = App(cfg, folder)
    port = port or _find_free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("=" * 56)
    print("  让绘本动起来")
    print("=" * 56)
    print("  工程目录：%s" % os.path.abspath(folder))
    print("  界面地址：%s" % url)
    print("  关掉这个窗口，界面就停了。")
    print("=" * 56)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0

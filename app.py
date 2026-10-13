"""让绘本动起来 —— 启动入口。

    python app.py                       启动界面（工程目录默认 ./我的动画）
    python app.py --folder 我的绘本       指定工程目录（可以直接指向绘本工坊那个目录）
    python app.py --check               不开界面，只做一次环境自检
    python app.py --plan                不开界面，只把时间轴算出来看一眼
    python app.py --example             把示例工程复制一份出来
    python app.py --clips               不开界面，直接出片段（配 --limit 先跑一页）
    python app.py --assemble            不开界面，直接合成成片
    python app.py --verify              对最新的成片跑一遍出厂校验
    python app.py --port 8770           指定端口
    python app.py --no-browser          不要自动打开浏览器

界面能干的事，命令行都能干，方便接到别的脚本里（比如定时批量出片）。
依赖：只有 Python 标准库。外部需要 ComfyUI（出片段）与 ffmpeg（合成）。
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from motion import config as cfgmod          # noqa: E402
from motion import project as P              # noqa: E402

DEFAULT_FOLDER = "我的动画"
EXAMPLE_DIR = os.path.join(HERE, "examples", "小狐狸的秋日远行")


def _setup_console():
    """Windows 控制台默认不是 UTF-8，中文会变乱码。

    顺手打开行缓冲：命令行模式下（尤其是把输出重定向到文件时）默认是全缓冲，
    进度会一直攒着不落盘，看起来像卡死了。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass


def _headless(folder):
    from motion import web
    return web.App(cfgmod.load(), folder)


def cmd_check(folder):
    app = _headless(folder)
    app.check()
    for row in app.st.log:
        print(row["text"])
    return 0


def cmd_plan(folder):
    app = _headless(folder)
    r = app.plan()
    for row in app.st.log:
        print(row["text"])
    return 0 if r.get("ok") else 1


def _drain(app):
    """命令行模式下把事件直接打到屏幕上，不经过界面那份日志。"""
    def on_event(evt):
        k = evt.get("kind")
        if k == "log":
            print("  " + str(evt.get("msg", "")))
        elif k == "clip_start":
            print("[%d/%d] %s 出片中…" % (evt["index"], evt["total"], evt["id"]))
        elif k == "clip_tick":
            print("  +%.0f 秒　显存 %s GB" % (evt.get("elapsed") or 0, evt.get("vram")))
        elif k == "clip_done":
            print("[%d/%d] %s ✅ %.0f 秒 / %d KB"
                  % (evt["index"], evt["total"], evt["id"], evt["seconds"], evt["kb"]))
        elif k == "skip":
            print("[%d/%d] %s 已有片段，跳过"
                  % (evt["index"], evt["total"], evt["id"]))
        elif k == "seg":
            print("  分段 %s  %.2f 秒" % (evt["id"], evt["seconds"]))
        elif k == "voice":
            print("  旁白 %s  %.2f 秒" % (evt["id"], evt["seconds"]))
        elif k == "error":
            print("✗ %s%s" % (evt.get("id", ""), evt.get("msg", "")))
    return on_event


def cmd_clips(folder, limit):
    from motion import project as P
    from motion import render as R
    from motion import workflow
    app = _headless(folder)
    per = workflow.estimate_seconds(app.cfg)
    data = P.load(folder)
    n = min(limit or len(data["pages"]), len(data["pages"]))
    print("出片段：%d 页　单段约 %.0f 秒　合计约 %.0f 分钟\n"
          % (n, per, per * n / 60))
    rep = R.Renderer(app.cfg, folder, _drain(app)).run(limit=limit or 0)
    if not rep.get("ok"):
        print("\n失败：%s" % rep.get("error"))
        return 1
    print("\n成功 %d/%d 页，用了 %.1f 分钟"
          % (rep.get("done", 0), rep.get("total", 0), rep.get("minutes", 0)))
    return 0


def cmd_assemble(folder, music=""):
    from motion import assemble as A
    from motion import media
    app = _headless(folder)
    print("工程：%s" % os.path.abspath(folder))
    print("ffmpeg：%s\n" % (media.tools(app.cfg)[0] or "没找到，先装 ffmpeg"))
    rep = A.Assembler(app.cfg, folder, _drain(app)).run(music=music or "")
    if not rep.get("ok"):
        print("\n失败：%s" % rep.get("error"))
        return 1
    print("\n成片：%s\n  时长 %.2f 秒　大小 %.1f MB　旁白 %d 条　字幕 %d 条"
          % (rep["film"], rep["seconds"], rep["mb"], rep["narration"],
             rep["subtitles"]))
    return 0


def cmd_verify(folder):
    from motion import verify as V
    app = _headless(folder)
    films = app.films()
    if not films:
        print("还没有成片，先跑一次 --assemble。")
        return 1
    r = V.check(app.cfg, films[0], on_event=_drain(app))
    return 0 if r.get("ok") else 1


def cmd_example(folder):
    if not os.path.isdir(EXAMPLE_DIR):
        print("示例目录不存在：%s" % EXAMPLE_DIR)
        return 1
    if os.path.exists(os.path.join(folder, cfgmod.PROJECT_NAME)):
        print("「%s」里已经有工程了，换个目录名再试。" % folder)
        return 1
    if not os.path.isdir(folder):
        os.makedirs(folder)
    for name in os.listdir(EXAMPLE_DIR):
        src = os.path.join(EXAMPLE_DIR, name)
        dst = os.path.join(folder, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    print("示例已复制到：%s" % os.path.abspath(folder))
    print("（示例里只有分镜、正文和运动描述，没有页图。")
    print("  页图要么用绘本工坊出，要么把你自己的图按 img/<页 id>.png 放进去。）")
    return 0


def main():
    _setup_console()
    ap = argparse.ArgumentParser(description="让绘本动起来：把一本绘本做成一集动画片")
    ap.add_argument("--folder", default=DEFAULT_FOLDER, help="工程目录")
    ap.add_argument("--port", type=int, default=0, help="服务端口（默认自动挑一个）")
    ap.add_argument("--check", action="store_true", help="只做环境自检")
    ap.add_argument("--plan", action="store_true", help="只算一遍时间轴")
    ap.add_argument("--example", action="store_true", help="复制示例工程")
    ap.add_argument("--clips", action="store_true", help="不开界面，直接出片段")
    ap.add_argument("--assemble", action="store_true", help="不开界面，直接合成成片")
    ap.add_argument("--verify", action="store_true", help="对最新成片做一次出厂校验")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 页（配 --clips）")
    ap.add_argument("--music", default="", help="配乐文件（配 --assemble）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)

    if args.example:
        return cmd_example(folder)
    if args.check:
        return cmd_check(folder)
    if args.plan:
        return cmd_plan(folder)
    if args.clips:
        return cmd_clips(folder, args.limit)
    if args.assemble:
        return cmd_assemble(folder, args.music)
    if args.verify:
        return cmd_verify(folder)

    cfg = cfgmod.load()
    P.ensure_dirs(folder)
    if not P.load(folder, auto_create=False):
        P.save(folder, P.from_book(folder))
        print("已新建工程：%s" % P.manifest_path(folder))
        if os.path.isdir(EXAMPLE_DIR):
            print("（examples/ 里有一份写好的示例，想看长什么样："
                  "python app.py --example）")
    else:
        data = P.load(folder)
        print("工程：%s（%d 个镜头）" % (folder, len(data["pages"])))

    from motion import web
    return web.run(cfg, folder, port=args.port or None,
                   open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())

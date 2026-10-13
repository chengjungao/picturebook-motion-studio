"""工程读写。

一个工程就是一个目录。里面**大部分东西是绘本工坊留下的**，本项目只往里面加几样：

    project.json        绘本工坊写的（书名、每页正文、分镜）  ← 只读，不碰
    img/<页 id>.png     绘本工坊出的页图                      ← 只读，不碰
    char_sheet.png      角色定妆照（同上，用不到但请留着）      ← 只读，不碰

    motion.json         ★ 本项目写的：每页一句「怎么动」
    voice/<页 id>.wav   旁白（你自己录、或别的工具生成）
    clips/<页 id>.mp4   片段（本项目出）
    build/              中间产物：分段、字幕、无音轨整片
    out/                成片

页 id 直接用绘本工坊那一套（`00-cover`、`01-autumn-comes`…），
所以页图不用改名、不用拷贝，两边的工程可以指同一个目录。
"""
import os

from . import config as cfgmod
from . import workflow

SUBDIRS = ("voice", "clips", "build", "out")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def norm_id(pid):
    """页图文件名用的短 id。片段与旁白也用它，省得两边不一致。"""
    return (pid or "").replace("-", "_")


def img_dir(folder):
    return os.path.join(folder, "img")


def voice_dir(folder):
    return os.path.join(folder, "voice")


def clips_dir(folder):
    return os.path.join(folder, "clips")


def build_dir(folder):
    return os.path.join(folder, "build")


def out_dir(folder):
    return os.path.join(folder, "out")


def manifest_path(folder):
    return os.path.join(folder, cfgmod.PROJECT_NAME)


def ensure_dirs(folder):
    for d in [img_dir(folder)] + [os.path.join(folder, s) for s in SUBDIRS]:
        if not os.path.isdir(d):
            os.makedirs(d)


def page_image(folder, pid):
    """找这一页的图。绘本工坊存成 img/<页 id>.png，也认 jpg。"""
    d = img_dir(folder)
    for name in (pid, norm_id(pid)):
        for ext in _IMG_EXT:
            p = os.path.join(d, name + ext)
            if os.path.exists(p):
                return p
    return ""


def clip_path(folder, pid):
    return os.path.join(clips_dir(folder), norm_id(pid) + ".mp4")


def clips_for(folder, pid):
    """这一页的片段。默认是 clips/<页 id>.mp4；也认 `…_c1.mp4` / `…_c2.mp4`
    这种带段号的老命名，方便读者把自己已有的片段直接丢进来用。"""
    import glob
    d = clips_dir(folder)
    one = clip_path(folder, pid)
    if os.path.exists(one):
        return [one]
    return sorted(glob.glob(os.path.join(d, norm_id(pid) + "_c*.mp4")))


def voice_path(folder, pid):
    return os.path.join(voice_dir(folder), norm_id(pid) + ".wav")


def find_voice(folder, pid):
    """旁白可以是 wav / mp3 / m4a，找得到就用。"""
    d = voice_dir(folder)
    for name in (norm_id(pid), pid):
        for ext in (".wav", ".mp3", ".m4a", ".flac"):
            p = os.path.join(d, name + ext)
            if os.path.exists(p):
                return p
    return ""


def find_book(folder):
    """读绘本工坊的那个 project.json（如果有）。"""
    p = os.path.join(folder, "project.json")
    if os.path.exists(p):
        return cfgmod.read_json(p, {}) or {}
    return {}


def from_book(folder):
    """从绘本工程生成一份 motion.json 初稿。

    正文照搬绘本里的文字（那就是最好的旁白稿），运动描述留空 —— 空的话
    出片时会套一句通用描述，不会跑不起来。想要画面动得贴切，就自己填，
    或者把每页的分镜丢给任意一个 AI，让它照着写一句。
    """
    book = find_book(folder)
    pages = []
    for p in book.get("pages") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        text = p.get("text") or []
        if isinstance(text, list):
            text = "".join(text)
        pages.append({
            "id": p["id"],
            "kind": p.get("kind") or "page",
            "text": (text or "").strip(),
            "motion": "",
        })
    if not pages:
        pages = new_pages()
    # 书名和副题绘本工坊是写在封面那一页上的，根上不一定有
    cover = next((p for p in book.get("pages") or []
                  if isinstance(p, dict) and p.get("kind") == "cover"), {})
    return {
        "title": book.get("title") or cover.get("title") or "我的绘本",
        "subtitle": book.get("subtitle") or cover.get("subtitle") or "",
        "byline": book.get("byline") or "",
        "created": "",
        "pages": pages,
    }


def new_pages():
    """空工程：一页封面、一页内页、一页封底，够把流程跑通。"""
    return [
        {"id": "00-cover", "kind": "cover", "text": "", "motion": ""},
        {"id": "01-page", "kind": "page", "text": "", "motion": ""},
        {"id": "02-back", "kind": "back", "text": "", "motion": ""},
    ]


def load(folder, auto_create=True):
    """读工程。没有 motion.json 就从绘本工程推一份出来，直接落盘。"""
    p = manifest_path(folder)
    data = cfgmod.read_json(p, None)
    if not isinstance(data, dict) or not data.get("pages"):
        if not auto_create:
            return None
        data = from_book(folder)
        save(folder, data)
    data.setdefault("pages", [])
    for pg in data["pages"]:
        pg.setdefault("kind", "page")
        pg.setdefault("text", "")
        pg.setdefault("motion", "")
    return data


def save(folder, data):
    ensure_dirs(folder)
    cfgmod.write_json(manifest_path(folder), data)
    return manifest_path(folder)


def page_by_id(data, pid):
    return next((p for p in data.get("pages") or [] if p.get("id") == pid), None)


def summary(folder, data):
    """给界面看的一份状态：每页图有没有、旁白有没有、片段有没有。"""
    rows = []
    for pg in data.get("pages") or []:
        pid = pg["id"]
        img = page_image(folder, pid)
        v = find_voice(folder, pid)
        c = clip_path(folder, pid)
        rows.append({
            "id": pid,
            "kind": pg.get("kind"),
            "text": pg.get("text") or "",
            "motion": pg.get("motion") or "",
            "has_image": bool(img),
            "image_name": os.path.basename(img) if img else "",
            "has_voice": bool(v),
            "voice_name": os.path.basename(v) if v else "",
            "has_clip": os.path.exists(c),
            "clip_kb": round(os.path.getsize(c) / 1024) if os.path.exists(c) else 0,
            "need_motion": not (pg.get("motion") or "").strip()
            and pg.get("kind") != "cover",
        })
    return rows


def motion_gaps(data):
    """哪些页还没填运动描述（封面除外，封面往往一句话就够）。"""
    return [p["id"] for p in (data.get("pages") or [])
            if not (p.get("motion") or "").strip() and p.get("kind") != "cover"]


def fill_missing_motion(data):
    """把空着的运动描述填成兜底那句，出片时就不会再提醒。"""
    n = 0
    for p in data.get("pages") or []:
        if not (p.get("motion") or "").strip():
            p["motion"] = workflow.FALLBACK_MOTION
            n += 1
    return n

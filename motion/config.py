"""配置读写。

配置放在项目根目录的 config.json，首次运行自动生成默认值。
所有可调项都集中在这里，改配置不用碰代码。

默认值不是拍脑袋定的，是一整轮实测跑出来的（见 docs/常见问题.md 里的口径）：
10 步 / cfg 5.0 / 241 帧一页一段，在 16GB 显存上跑得下、画面也不漂。
"""
import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

# 工程里属于"我们"的那个文件。绘本工坊的 project.json 我们只读不写。
PROJECT_NAME = "motion.json"

DEFAULT = {
    "comfy_url": "http://127.0.0.1:8188",
    "models": {
        "unet": "wan2.2_ti2v_5B_fp16.safetensors",
        "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "vae": "wan2.2_vae.safetensors",
    },
    "video": {
        # 一页一段，整段 10.04 秒。拆成两段再拼会有接缝，而且漂移会叠加。
        "width": 1280,
        "height": 704,
        "fps": 24,
        "frames": 241,
        "steps": 10,
        "cfg": 5.0,
        "shift": 8.0,
        "base_seed": 20261013,
    },
    "film": {
        "lead": 0.5,             # 旁白开始前留白
        "tail": 0.5,             # 旁白结束后留白
        "cover_sec": 6.0,        # 封面镜头固定时长
        "min_shot": 6.0,         # 每页最短镜头
        "fade_in": 0.8,
        "fade_out": 1.5,
        "music_gain_db": -13.0,  # 配乐垫底相对旁白的电平
        "font_dir": "C:/Windows/Fonts",
        "font_name": "KaiTi",
        "title_size": 88,
        "subtitle_size": 38,
        "body_size": 46,
        "wrap": 17,              # 字幕每行字数
    },
    "tts": {
        # none = 只用你自己放进 voice/ 的音频；kokoro = 用本机 Kokoro 一键生成
        "engine": "none",
        "kokoro_python": "",
        "kokoro_model_dir": "",
        "voice": "zf_001",
        "speed": 0.9,
    },
    "ffmpeg": {"ffmpeg": "", "ffprobe": ""},
    "browser_path": "",
}


def _merge(base, override):
    """按 key 递归合并，保证新增的默认项在旧配置里也能补上。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    if not os.path.exists(CONFIG_PATH):
        save(DEFAULT)
        return dict(DEFAULT)
    try:
        with io.open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return dict(DEFAULT)
    return _merge(DEFAULT, raw)


def save(cfg):
    with io.open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
    return CONFIG_PATH


def save_and_reload(cfg):
    """保存前先补齐默认项，避免界面漏传字段导致配置残缺。"""
    merged = _merge(DEFAULT, cfg)
    save(merged)
    return merged


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    """带父目录创建与中文保留的 JSON 落盘。"""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))

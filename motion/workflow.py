"""Wan 2.2 图生视频的 ComfyUI 工作流。

节点链（API 格式）：

    UNETLoader ──► ModelSamplingSD3 ──┐
                                      ├─► KSampler ──► VAEDecode ──► CreateVideo ──► SaveVideo
    CLIPLoader ──► CLIPTextEncode ×2 ─┘        ▲
    VAELoader ─────────────────────────────────│
    LoadImage ──► Wan22ImageToVideoLatent ─────┘

两处容易接错：

* `Wan22ImageToVideoLatent` **只吐 latent**，正向/负向提示词要直接从
  `CLIPTextEncode` 接到 `KSampler`，别再往这个节点里塞。
* `CreateVideo` 与 `SaveVideo` 是分开的两个节点，`SaveVideo` 必须同时给
  `format="mp4"` 和 `codec="h264"`，少一个都存不出文件。
"""
from . import media

# 负面提示词：把"画崩"的常见形态列进去。cfg 必须大于 1 它才起作用
# （蒸馏版权重只能在 cfg 1.0 下跑，那样负面提示词等于没有，实测会把整张画重绘）。
NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
    "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
    "杂乱的背景，三条腿，背景人很多，倒着走"
)

# 每页运动描述后面统一接这句，锁住画风
STYLE_SUFFIX = ("柔和水彩绘本插画风格，温暖秋色，柔和光线，画面稳定，镜头固定，"
                "画面内没有任何文字。")

# 一整段 10 秒时，动作要铺满，不然模型两秒做完就僵住了
LONG_TAIL = "整个动作缓慢而持续地铺满整段画面。"

# 页面没填运动描述时的兜底
FALLBACK_MOTION = ("画面里的空气轻轻流动，树叶一片一片缓缓飘落，主角的尾巴慢慢摆动，"
                   "光线柔和地晃动。")

REQUIRED_NODES = [
    "UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "LoadImage",
    "ModelSamplingSD3", "Wan22ImageToVideoLatent", "KSampler", "VAEDecode",
    "CreateVideo", "SaveVideo",
]


def positive_text(motion):
    """把一页的运动描述拼成完整正向提示词。"""
    m = (motion or "").strip() or FALLBACK_MOTION
    if not m.endswith(("。", "！", "？", ".")):
        m += "。"
    return m + LONG_TAIL + STYLE_SUFFIX


def build(cfg, image_name, positive, seed, prefix):
    """按 API 格式搭一张图。image_name 是图在 ComfyUI input 区里的文件名。"""
    v = cfg["video"]
    m = cfg["models"]
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": m["clip"], "type": "wan"}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["3", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": NEGATIVE, "clip": ["3", 0]}},
        "6": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "7": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "8": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["6", 0], "width": v["width"], "height": v["height"],
                         "length": v["frames"], "batch_size": 1,
                         "start_image": ["7", 0]}},
        "2": {"class_type": "ModelSamplingSD3",
              "inputs": {"model": ["1", 0], "shift": v["shift"]}},
        "9": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": v["steps"], "cfg": v["cfg"],
                         "sampler_name": "euler", "scheduler": "simple",
                         "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["8", 0], "denoise": 1.0,
                         "model": ["2", 0]}},
        "10": {"class_type": "VAEDecode",
               "inputs": {"samples": ["9", 0], "vae": ["6", 0]}},
        "11": {"class_type": "CreateVideo",
               "inputs": {"images": ["10", 0], "fps": float(v["fps"])}},
        "12": {"class_type": "SaveVideo",
               "inputs": {"video": ["11", 0], "filename_prefix": prefix,
                          "format": "mp4", "codec": "h264"}},
    }


def prep_image(folder, pid, img_path, cfg):
    """把页图统一成出片尺寸，返回可上传的临时文件路径。

    不装 Pillow：缩图交给 ffmpeg，看图本来是多大都行，统一到 1280×704 更稳。
    """
    import os
    v = cfg["video"]
    src = media.probe(img_path, cfg)
    sv, _a = media.streams(src)
    if (str(sv.get("width")) == str(v["width"])
            and str(sv.get("height")) == str(v["height"])):
        return img_path
    d = os.path.join(folder, "build")
    if not os.path.isdir(d):
        os.makedirs(d)
    dst = os.path.join(d, "_fit_%s.png" % pid)
    return media.scale_to(img_path, dst, v["width"], v["height"], cfg)


def seconds_per_clip(cfg):
    v = cfg["video"]
    return round(v["frames"] / float(v["fps"]), 3)


def estimate_seconds(cfg):
    """单段耗时估算。

    标定来自实测：基础 fp16 权重、10 步 × 241 帧 = 480 秒，
    即每「步 × 帧」约 0.1264 秒。注意力随帧数超线性上涨，所以只能按这个折算。
    """
    v = cfg["video"]
    return v["steps"] * v["frames"] * 0.1264

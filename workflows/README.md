# 这张图是干什么的

`页图动起来.api.json` 是**工具真正提交给 ComfyUI 的那张图**，
直接从 `motion/workflow.py` 里导出来的，不是手抄的 —— 所以它和代码永远一致。

放着它有两个用处：

1. **想改参数、加节点**（比如换采样器、加个色彩校正），照着文件改
   `motion/workflow.py` 里对应的字段，比在界面里连一遍再截图靠谱。
2. **想看懂这一页到底怎么生成**，直接读它，比读代码省事。

## 它长什么样

12 个节点，链路是：

```
UNETLoader ──► ModelSamplingSD3 ──┐
                                  ├─► KSampler ──► VAEDecode ──► CreateVideo ──► SaveVideo
CLIPLoader ──► CLIPTextEncode ×2 ─┘        ▲
VAELoader ─────────────────────────────────│
LoadImage ──► Wan22ImageToVideoLatent ─────┘
```

两处容易接错，自己改的时候留意：

* `Wan22ImageToVideoLatent` **只吐 latent**。正向和负向提示词要直接从
  `CLIPTextEncode` 接到 `KSampler`，别再往这个节点里塞 `positive` / `negative`。
* `CreateVideo` 和 `SaveVideo` 是**分开的两个节点**，`SaveVideo` 必须同时给
  `format: "mp4"` 和 `codec: "h264"`，少一个都存不出文件。

## 这是 API 格式，不是界面格式

界面上「另存为」导出的那套带 `nodes` / `links` / `pos` 的是**界面格式**，
和这份不一样。这份是给程序读的，直接 POST 到
`http://127.0.0.1:8188/prompt` 就能跑：

```bash
curl -X POST http://127.0.0.1:8188/prompt \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": $(cat 页图动起来.api.json)}"
```

（前提是 `LoadImage` 里那个文件名已经在 ComfyUI 的 input 区里了。
本项目是运行时用 HTTP 上传页图的，所以文件名会变。）

## 关键参数在哪

| 参数 | 值 | 为什么 |
|---|---|---|
| `KSampler.steps` | 10 | 20 步和 10 步画面肉眼没区别，漂移 6.64 对 5.83，时间省一半 |
| `KSampler.cfg` | 5.0 | **必须大于 1**，否则负面提示词失效，模型会把整张页图重画 |
| `ModelSamplingSD3.shift` | 8.0 | Wan 2.2 的推荐值 |
| `Wan22ImageToVideoLatent.length` | 241 | ＝ 10.04 秒 @ 24fps。一页一段，不拼接 |
| `KSampler.sampler_name` | euler | 配合 `scheduler: simple` |

"""预热本机 Ollama 的两个模型，避免冷启成本落到第一个真实用户请求上。

不预热也能跑，但首次调用 qwen3.5:9b 实测要 ~9s、bge-m3 ~3.6s，
这些时间会直接计入用户的首 token 延迟。

用法：
    C:\\Python312\\python.exe -X utf8 scripts\\warmup_models.py
"""

import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA = "http://127.0.0.1:11434"

WARMUPS = [
    ("qwen3.5:9b  (LLM)", "/api/chat",
     {"model": "qwen3.5:9b", "think": False, "stream": False,
      "messages": [{"role": "user", "content": "hi"}]}),
    ("bge-m3       (向量)", "/api/embeddings",
     {"model": "bge-m3:latest", "prompt": "等待期"}),
    ("bge-m3       (向量)", "/api/embeddings",
     {"model": "bge-m3:latest", "prompt": "犹豫期"}),
    ("qwen3.5:9b  (LLM)", "/api/chat",
     {"model": "qwen3.5:9b", "think": False, "stream": False,
      "messages": [{"role": "user", "content": "你好"}]}),
]


def post(path, payload, timeout=300):
    req = urllib.request.Request(
        OLLAMA + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return time.time() - t


def resident():
    with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10) as r:
        return [m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])]


def main() -> int:
    try:
        print(f"预热前驻留: {resident()}")
    except Exception as e:  # noqa: BLE001
        print(f"[x] 连不上 Ollama: {e}")
        print("    先按启动手册 §2.1 起 Ollama")
        return 2

    print()
    for label, path, payload in WARMUPS:
        print(f"  {label:<22} {post(path, payload):6.2f}s")

    print(f"\n预热后驻留: {resident()}")
    n = len(resident())
    if n < 2:
        print("\n[!] 两个模型没有同时驻留 —— 检查 OLLAMA_MAX_LOADED_MODELS 是否设到了 serve 进程")
        return 1
    print("\nOK，两个模型均已驻留，后续请求不会再付加载成本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
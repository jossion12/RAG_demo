"""分组件实测耗时：Embedding / 向量检索 / Rerank / LLM 预填充。

为什么要拆开测：端到端 0.95~9.68s 波动太大，单看总数无法归因。
需求 §4.1 要求「检索+Rerank ≤800ms、首 token ≤2s」，
只有把四段分别计时，才能知道该优化哪一段。

用法：
    C:\\Python312\\python.exe scripts\\latency_breakdown.py
"""

import json
import statistics
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA = "http://127.0.0.1:11434"
TEI = "http://127.0.0.1:9999"
EMB_MODEL = "bge-m3:latest"
LLM_MODEL = "qwen3.5:9b"
RERANK_MODEL = "bge-reranker-v2-m3"

QUERY = "重疾险的犹豫期是多久？"
N = 5


def post_json(url, payload, timeout=300):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
    return body, time.time() - t0


def timed(label, fn):
    samples = []
    extra = None
    for _ in range(N):
        r, dt = fn()
        samples.append(dt)
        if extra is None:
            extra = r
    print(f"{label:<28} 中位 {statistics.median(samples):6.3f}s  "
          f"min {min(samples):6.3f}s  max {max(samples):6.3f}s")
    return statistics.median(samples), extra


def emb():
    def f():
        return post_json(f"{OLLAMA}/api/embeddings",
                         {"model": EMB_MODEL, "prompt": QUERY})
    return f


def rerank(docs):
    def f():
        return post_json(f"{TEI}/rerank",
                         {"model": RERANK_MODEL, "query": QUERY,
                          "texts": docs, "top_n": len(docs)})
    return f


def llm_prefill(ntok):
    # 用约 ntok 字的填充文本模拟真实 prompt 体量，测预填充而非生成
    filler = "保险条款正文片段。" * max(1, ntok // 8)
    def f():
        return post_json(f"{OLLAMA}/api/chat",
                         {"model": LLM_MODEL, "think": False, "stream": False,
                          "options": {"num_predict": 1},
                          "messages": [{"role": "user", "content": filler + "\n只回答1。"}]})
    return f


def main() -> int:
    print(f"query = {QUERY}")
    print(f"每项跑 {N} 次\n" + "-" * 62)

    t_emb, _ = timed("① Embedding (Ollama bge-m3)", emb())
    print()

    _, rr = timed("② Rerank (TEI, 8 条)", rerank([f"保险条款片段 {i}，等待期与犹豫期规定。" for i in range(8)]))
    print()

    for n in (500, 2000, 4000):
        timed(f"③ LLM 预填充 ~{n}字", llm_prefill(n))
    print("-" * 62)

    print("\n对照需求 §4.1：")
    print(f"  检索+Rerank 合计约 {t_emb:.3f}s + （②）  -> 目标 ≤0.800s")
    print("  首 token 目标 ≤2s（端到端实测见 quick_probe 的 time_to_first_token）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
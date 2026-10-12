"""手动复现 Dify 的检索链路，验证「投保后多久内身故赔钱？」这类跨表述问题。

流程与 Dify 一致：
  1. bge-m3 向量化 query
  2. weaviate nearVector 取候选（对应向量检索分支）
  3. TEI bge-reranker-v2-m3 重排（对应 reranking_enable）
  4. score_threshold=0.5 过滤

在 Dify worker 容器内运行（能同时访问 weaviate / ollama / tei）。
"""

import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA = "http://host.docker.internal:11434"
TEI = "http://tei-rerank:80"
WEAVIATE = "http://weaviate:8080"
CLASS = "Vector_index_b840bb7a_6289_4b5e_a09a_55e61e74043c_Node"
EMBED_MODEL = "bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
THRESHOLD = 0.5
CANDIDATES = 20


def post(url, payload, headers=None, timeout=180):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def gql(query, key):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        WEAVIATE + "/v1/graphql",
        data=json.dumps({"query": query}).encode("utf-8"),
        headers=headers,
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def run(query_text):
    print("=" * 78)
    print(f"Q: {query_text}")
    print("=" * 78)

    # 1) 向量化
    emb = post(OLLAMA + "/api/embed", {"model": EMBED_MODEL, "input": query_text})
    vec = emb["embeddings"][0]
    print(f"[1] bge-m3 向量维度 = {len(vec)}")

    # 2) 向量召回
    gql_q = f"""{{ Get {{ {CLASS}(nearVector: {{vector: {json.dumps(vec)}}}, limit: {CANDIDATES}) {{
        text document_id chunk_index page dataset_id source }} }} }}"""
    res = gql(gql_q, WKEY)
    objs = (res.get("data", {}).get("Get", {}).get(CLASS)) or []
    if res.get("errors"):
        print("[x] weaviate 错误:", res["errors"][:1])
        return
    print(f"[2] weaviate 召回候选 = {len(objs)} 条")

    if not objs:
        print("[!] 候选为空 -> 无论阈值多少都会被拒答")
        return

    texts = [o["text"] for o in objs]

    # 3) 重排
    # TEI 1.2 的 /rerank 返回裸数组 [{index, score}, ...]，
    # 不是 {"results": [...]}。两种都兼容。
    rr = post(TEI + "/rerank", {"query": query_text, "texts": texts, "top_n": len(texts)})
    results = rr["results"] if isinstance(rr, dict) and "results" in rr else rr
    ranked = sorted(results, key=lambda r: -r["score"])
    print(f"[3] rerank 完成，最高分 = {ranked[0]['score']:.4f}\n")

    print(f"{'排名':<5}{'分数':<10}{'过阈值':<9}{'来源/片段'}")
    print("-" * 78)
    passed = 0
    for i, r in enumerate(ranked[:8], 1):
        o = objs[r["index"]]
        ok = "✓" if r["score"] >= THRESHOLD else "✗ 被滤掉"
        if r["score"] >= THRESHOLD:
            passed += 1
        snippet = " ".join(o["text"].split())[:58]
        print(f"{i:<6}{r['score']:<10.4f}{ok:<10}…{snippet}…")

    print("-" * 78)
    print(f"阈值 {THRESHOLD} 过滤后剩 {passed} 条 "
          f"{'-> 会进入 LLM 生成' if passed else '-> 应拒答（未找到相关信息）'}")

    # 对照：阈值关闭时（score_threshold=0.0，即修复前的行为）会进多少条
    print(f"对照：若阈值=0.0（修复前），{len(objs)} 条全部进 LLM，"
          f"最低分 {ranked[-1]['score']:.4f}")
    print()


if __name__ == "__main__":
    import os
    # weaviate 容器 AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true，认证可省
    WKEY = os.environ.get("WKEY", "")
    queries = sys.argv[1:] or [
        "投保后多久内身故赔钱？",
        "住院医疗险的等待期是多久？",
        "重疾险的犹豫期是多久？退保扣多少钱？",
        "这款产品的年化收益率是多少？",
    ]
    for q in queries:
        try:
            run(q)
        except Exception as e:  # noqa: BLE001
            print(f"[x] 失败: {type(e).__name__}: {e}\n")

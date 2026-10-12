"""top_k 实验：候选池放大后，「90 日等待期」段能否进池并被 rerank 选中。

背景：知识库级阈值降到 0.3 后确实多放行了 1 条（0.3383），
但那条是另一份文档的「现金价值」段，不是等待期。
纯向量检索（limit 20）能把「新增被保险人 90 日的等待期」排到第 3 名，
说明它在向量空间里并不远——是 hybrid_search 的 top_k=8 把它挡在池外。

这里逐档放大 top_k，看它进池后拿到多少分、答案是否变化。

用法：
    C:\\Python312\\python.exe scripts\\topk_experiment.py
"""

import json
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = "docker-db_postgres-1"
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
APP_ID = "f5939e3a-5fd7-43e7-8824-54d6ed1bc8da"
BASE = "http://localhost/v1/chat-messages"
QUERY = "住院医疗险的等待期是多久？"
MARK = "90"


def psql(sql):
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
    return p.stdout.decode("utf-8").strip()


def set_top_k(value):
    raw = psql(f"select retrieval_model from datasets where id='{DATASET_ID}';")
    rm = json.loads(raw)
    rm["top_k"] = value
    payload = json.dumps(rm, ensure_ascii=False).replace("'", "''")
    psql(f"update datasets set retrieval_model = '{payload}' where id='{DATASET_ID}';")


def get_key():
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c",
                        "select token from api_tokens where id::text like '21e9da6b%' "
                        f"and app_id='{APP_ID}';"], capture_output=True)
    return p.stdout.decode("utf-8").strip()


def probe(tag: str):
    key = get_key()
    body = {"inputs": {}, "query": QUERY, "response_mode": "blocking",
            "user": f"topk-{tag}-{int(time.time())}"}
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8")), time.time() - t0


def main() -> int:
    print(f"查询: {QUERY}")
    print(f"知识库级阈值固定 0.3，只改 top_k\n" + "=" * 70)
    try:
        for value in (8, 15, 30):
            set_top_k(value)
            d, dt = probe(value)
            res = (d.get("metadata") or {}).get("retriever_resources") or []
            hit = [i for i, x in enumerate(res, 1)
                   if MARK in (x.get("content") or "") and "等待期" in (x.get("content") or "")]
            print(f"\ntop_k = {value}   ({dt:.2f}s)")
            print(f"  引用 {len(res)} 条, 分数 {[round(x.get('score') or 0, 4) for x in res]}")
            print(f"  命中「{MARK}日…等待期」的条目: {hit or '无'}")
            for i in hit:
                seg = (res[i - 1].get("content") or "").replace("\n", " ")
                idx = seg.find("等待期")
                print(f"    -> [{i}] …{seg[max(0, idx - 90):idx + 60]}…")
            print(f"  答案: {(d.get('answer') or '').replace(chr(10), ' ')[:130]}")
    finally:
        set_top_k(8)
        print("\n已恢复 top_k = 8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
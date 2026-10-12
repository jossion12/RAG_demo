"""决定性实验：节点级阈值 vs 知识库级阈值，到底哪个在起作用。

现象：节点级 score_threshold 从 0.5 改到 0.3 再改到 0.0，
返回条数恒为 2（分数都是 0.5935），而知识库级 score_threshold = 0.5。
两种解释：
  (A) 运行时用的是知识库级配置，节点级根本没被采纳
  (B) 节点级被采纳了，但候选池本来就只有 2 条

上一个实验（阈值 0.0 仍 2 条）已排除 (B) 的一部分，但仍需与 (A) 区分。
这里直接改知识库级阈值：若条数随之变化，就是 (A)。

改完务必恢复原值 0.5。

用法：
    C:\\Python312\\python.exe scripts\\dataset_threshold_experiment.py
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
ORIGINAL = 0.5


def psql(sql):
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
    return p.stdout.decode("utf-8").strip()


def set_dataset_threshold(value):
    raw = psql(f"select retrieval_model from datasets where id='{DATASET_ID}';")
    rm = json.loads(raw)
    rm["score_threshold"] = value
    payload = json.dumps(rm, ensure_ascii=False).replace("'", "''")
    psql(f"update datasets set retrieval_model = '{payload}' where id='{DATASET_ID}';")
    cur = json.loads(psql(f"select retrieval_model from datasets where id='{DATASET_ID}';"))
    print(f"    知识库级 score_threshold -> {cur['score_threshold']}")


def get_key():
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c",
                        "select token from api_tokens where id::text like '21e9da6b%' "
                        f"and app_id='{APP_ID}';"], capture_output=True)
    return p.stdout.decode("utf-8").strip()


def probe():
    key = get_key()
    body = {"inputs": {}, "query": QUERY, "response_mode": "blocking",
            "user": f"ds-thr-{int(time.time())}"}
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    print(f"查询: {QUERY}")
    print(f"节点级阈值固定为 0.3（上一轮已写入），只改知识库级\n" + "=" * 66)
    try:
        for th in (0.5, 0.3, 0.0):
            print(f"\n知识库级阈值 = {th}")
            set_dataset_threshold(th)
            d = probe()
            res = (d.get("metadata") or {}).get("retriever_resources") or []
            scores = [round(x.get("score") or 0, 4) for x in res]
            print(f"    -> {len(res)} 条, 分数 {scores}")
            ans = (d.get("answer") or "").replace("\n", " ")
            print(f"    -> 答案: {ans[:110]}")
            if any("90" in ((x.get("content") or "")) for x in res):
                print("    -> [!] 命中含 90 日的段落")
    finally:
        print(f"\n恢复知识库级阈值 = {ORIGINAL}")
        set_dataset_threshold(ORIGINAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
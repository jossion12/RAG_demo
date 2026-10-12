"""一次性实验：阈值 0.0 时检索返回多少条，用来区分两种可能。

现象：阈值 0.5 与 0.3 都只返回 2 条（都是 0.5935）。
但手工复现链路时，0.3383 的「90 日等待期」段排在 rerank 第 3 名（在 top-8 内）。
两种解释：
  (A) 候选池本来就只有 2 条 -> 阈值改什么都没用，要去查召回侧
  (B) 候选池 8 条，但阈值作用在 rerank 之前的向量分上 -> 阈值有用，得继续调
把阈值临时置 0 再跑一次，就能二选一。

用法：
    C:\\Python312\\python.exe scripts\\threshold_experiment.py
"""

import base64
import json
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = "docker-db_postgres-1"
WORKFLOW_ID = "3ab42de2-6ae6-43c3-a2ee-cf7489beaa6d"
APP_ID = "f5939e3a-5fd7-43e7-8824-54d6ed1bc8da"
BASE = "http://localhost/v1/chat-messages"
QUERY = "住院医疗险的等待期是多久？"


def psql(sql):
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
    return p.stdout.decode("utf-8").strip()


def set_threshold(value):
    """直接改已发布版本的节点级阈值。"""
    raw = psql(f"select graph from workflows where id='{WORKFLOW_ID}';")
    g = json.loads(raw)
    for n in g["nodes"]:
        if n["data"].get("type") == "knowledge-retrieval":
            n["data"]["multiple_retrieval_config"]["score_threshold"] = value
    payload = base64.b64encode(json.dumps(g, ensure_ascii=False).encode("utf-8")).decode()
    psql(f"update workflows set graph = convert_from(decode('{payload}','base64'),'UTF8')::jsonb "
         f"where id='{WORKFLOW_ID}';")
    cur = psql("select n->'data'->'multiple_retrieval_config'->>'score_threshold' "
               f"from workflows w, jsonb_array_elements((w.graph::jsonb)->'nodes') n "
               f"where w.id='{WORKFLOW_ID}' and n->'data'->>'type'='knowledge-retrieval';")
    print(f"    已写入 score_threshold={value}，回读={cur}")


def get_key():
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c",
                        "select token from api_tokens where id::text like '21e9da6b%' "
                        f"and app_id='{APP_ID}';"], capture_output=True)
    return p.stdout.decode("utf-8").strip()


def probe():
    key = get_key()
    body = {"inputs": {}, "query": QUERY, "response_mode": "blocking", "user": f"thr-exp-{int(time.time())}"}
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    original = 0.3
    print(f"查询: {QUERY}\n" + "=" * 66)
    try:
        for th in (0.0, 0.3):
            print(f"\n阈值 = {th}")
            set_threshold(th)
            d = probe()
            res = (d.get("metadata") or {}).get("retriever_resources") or []
            print(f"    -> 返回 {len(res)} 条, 分数 "
                  f"{[round(x.get('score') or 0, 4) for x in res]}")
            print(f"    -> 答案: {(d.get('answer') or '')[:90]}")
            if th == 0.0 and len(res) > 2:
                print("\n    结论: 候选池 >2，阈值确实在起作用 -> 是阈值问题")
            elif th == 0.0:
                print("\n    结论: 阈值=0 也只有 2 条 -> 问题在召回侧，不在阈值")
    finally:
        print(f"\n恢复阈值 = {original}")
        set_threshold(original)
    return 0


if __name__ == "__main__":
    sys.exit(main())
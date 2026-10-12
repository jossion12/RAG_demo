# -*- coding: utf-8 -*-
"""核对：扫描里 M-02 在 th=0.10 显示 0 条，但直接打同一条查询却拿到 1 条（0.139）。

这种「同一参数、同一查询、两次结果不同」必须查清楚，
否则后面所有基于扫描的结论都不可信。逐条对比扫描存档与实时探测。
"""
import io
import json
import os
import subprocess
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
KEY_FILE = "C:\\Users\\Administrator\\.dify_dataset_key"


def base_config():
    p = subprocess.run(["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres",
                        "-d", "dify", "-t", "-A", "-c",
                        "select retrieval_model from datasets where id='%s';" % DATASET_ID],
                       capture_output=True)
    return json.loads(p.stdout.decode("utf-8").strip())


def live(tok, query, rm):
    req = urllib.request.Request(
        "http://localhost/v1/datasets/%s/retrieve" % DATASET_ID,
        data=json.dumps({"query": query, "retrieval_model": rm}).encode("utf-8"),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8"))
    return [(x.get("score") or 0, ((x.get("segment") or {}).get("document") or {}).get("name", ""),
             ((x.get("segment") or {}).get("content") or "")[:60].replace("\n", " "))
            for x in (d.get("records") or [])]


def main():
    tok = io.open(KEY_FILE, encoding="utf-8").read().strip()
    cases = {c["id"]: c for c in json.load(
        io.open(os.path.join(ROOT, "eval", "eval_cases_v2.json"), encoding="utf-8"))["cases"]}
    sweep = json.load(io.open(os.path.join(ROOT, "eval", "runs", "retrieval_sweep.json"),
                              encoding="utf-8"))
    stored = {}
    for r in sweep["results"]:
        for row in r["rows"]:
            stored[(r["config"]["score_threshold"], row["id"])] = row

    print("数据库当前 retrieval_model.score_threshold =", base_config()["score_threshold"])
    print("扫描存档里的 th=0.10 配置:", json.dumps(
        [r["config"] for r in sweep["results"] if r["config"]["score_threshold"] == 0.10][0],
        ensure_ascii=False))
    print()
    for cid in ("M-02", "M-09", "G-01"):
        row = stored.get((0.10, cid))
        print("扫描存档 %-5s n=%s error=%s clause_hit=%s" % (cid, row.get("n"), row.get("error"),
                                                            row.get("clause_hit")))
        rm = json.loads(json.dumps(base_config()))
        rm["score_threshold"] = 0.10
        rm["top_k"] = 30
        got = live(tok, cases[cid]["question"], rm)
        print("实时探测 %-5s -> %d 条 %s" % (cid, len(got), [round(s, 4) for s, _, _ in got[:5]]))
        for s, doc, txt in got[:3]:
            print("        %.4f  %s | %s" % (s, doc[:26], txt))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
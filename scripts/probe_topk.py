# -*- coding: utf-8 -*-
"""单点探针：验证 top_k > 30 到底是「真的召回为空」还是「报错被吞」。

背景：扫描里 top_k=40/60 全部返回 0 条、平均条数 0.0、噪声 0，
这三个数同时为 0 更像是「一次都没成功」而不是「召回到空集」。
用同一组参数逐个 top_k 直接打一次，把 HTTP 状态和异常原样打出来。
"""
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
KEY_FILE = "C:\\Users\\Administrator\\.dify_dataset_key"
QUERY = "住院医疗险要自己先垫多少钱？"


def base_config():
    p = subprocess.run(["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres",
                        "-d", "dify", "-t", "-A", "-c",
                        "select retrieval_model from datasets where id='%s';" % DATASET_ID],
                       capture_output=True)
    return json.loads(p.stdout.decode("utf-8").strip())


def main():
    tok = io.open(KEY_FILE, encoding="utf-8").read().strip()
    base = base_config()
    for k in (30, 31, 32, 33, 34, 35, 36, 38, 40, 50):
        rm = json.loads(json.dumps(base))
        rm["score_threshold"] = 0.10
        rm["top_k"] = k
        req = urllib.request.Request(
            "http://localhost/v1/datasets/%s/retrieve" % DATASET_ID,
            data=json.dumps({"query": QUERY, "retrieval_model": rm}).encode("utf-8"),
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode("utf-8"))
            recs = d.get("records") or []
            scores = [round(x.get("score") or 0, 3) for x in recs[:5]]
            print("top_k=%-3d -> HTTP 200, %d 条, 前5分数 %s" % (k, len(recs), scores))
        except urllib.error.HTTPError as e:
            print("top_k=%-3d -> HTTP %s : %s" % (k, e.code, e.read().decode("utf-8", "replace")[:220]))
        except Exception as e:                                     # noqa: BLE001
            print("top_k=%-3d -> %s: %s" % (k, type(e).__name__, str(e)[:180]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
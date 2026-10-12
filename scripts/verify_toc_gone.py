# -*- coding: utf-8 -*-
"""重灌知识库后的检索层验证：目录噪声是否真的消失了。

为什么单独一个探针：eval 要跑满 52 条、10 分钟以上，而「目录页有没有被
挤进候选」在检索层 5 条就能判。判据用 eval/run_iteration.py 自己的
toc_noise()，不用另写一套 —— 两边口径必须一致，否则评测报告里的
「目录页噪声」和这里的结论对不上。

用法：
    python scripts/verify_toc_gone.py
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import requests  # noqa: E402
import run_iteration as R  # noqa: E402  复用真判据 toc_noise()

DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"

# 取 r004 里 toc_noise > 0 的题 —— 原来吵得最凶的几条，最该先被治好
QS = [
    "重疾险的犹豫期是多久？",
    "住院医疗险的等待期是多少天？",
    "哪些情况保险公司不赔？",
    "免赔额是多少？",
    "重疾险忘交保费了会怎么样？有宽限期吗？",
    "搬家换了地址，保单上的联系方式要怎么办？",
]


def psql(sql):
    p = subprocess.run(["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres",
                        "-d", "dify", "-t", "-A", "-c", sql], capture_output=True)
    return p.stdout.decode("utf-8").strip()


def main():
    tok = open(os.path.join(os.path.expanduser("~"), ".dify_dataset_key"),
               encoding="utf-8").read().strip()
    rm = json.loads(psql("select retrieval_model from datasets where id='%s';" % DATASET_ID))
    print("检索参数: top_k=%s threshold=%s rerank=%s\n"
          % (rm.get("top_k"), rm.get("score_threshold"),
             (rm.get("reranking_model") or {}).get("reranking_model_name")))

    noisy = 0
    for q in QS:
        r = requests.post("http://localhost/v1/datasets/%s/retrieve" % DATASET_ID,
                          headers={"Authorization": "Bearer " + tok},
                          json={"query": q, "retrieval_model": rm}, timeout=180).json()
        recs = r.get("records") or []
        cites = [{"snippet": (x.get("segment") or {}).get("content", ""),
                  "label": (x.get("segment") or {}).get("document", {}).get("name", "")}
                 for x in recs]
        n = R.toc_noise(cites)
        noisy += 1 if n else 0
        print("Q: %s" % q)
        print("   召回 %d 条, toc_noise=%d %s" % (len(recs), n, "❌" if n else "✅"))
        for x in recs[:3]:
            seg = x.get("segment") or {}
            print("   %.4f %-22s %s" % (
                x.get("score", 0), seg.get("document", {}).get("name", "?")[:22],
                " ".join(seg.get("content", "").split())[:60]))
        print()

    print("=" * 60)
    print("目录噪声: %d/%d 条 (r004 基线这些题全部 >0)" % (noisy, len(QS)))
    return 0 if noisy == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""A/B 探针：验证「节点级 score_threshold」是不是真正生效的那一档。

用两条在基线里 0 引用（被阈值砍光）的用例，调一次、看引用条数变没变。
比跑整轮快得多，而且能把「生效位置」这种问题一次问清。
"""
import io
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, "eval")
import run_iteration as R                                              # noqa: E402

PROBES = ["M-02", "M-09", "G-03", "G-04", "M-11"]   # 基线里被判 over_refusal 的几条


def main():
    cases = {c["id"]: c for c in json.load(
        io.open("eval/eval_cases_v2.json", encoding="utf-8"))["cases"]}
    base = "http://localhost:8080"
    for cid in PROBES:
        c = cases[cid]
        t0 = time.time()
        try:
            res = R.call_frontend_sse(base, c["question"])
        except Exception as e:                                        # noqa: BLE001
            print("%-6s 请求失败: %s" % (cid, e))
            continue
        cites = res.get("citations") or []
        passed, cls, info = R.classify(c, res)
        print("%-6s 引用 %d 条  首条分 %s  耗时 %dms  -> %s (%s)"
              % (cid, len(cites),
                 round(cites[0]["score"], 4) if cites else "-",
                 int((time.time() - t0) * 1000),
                 "✅ 过" if passed else "❌ %s" % cls,
                 info.get("why", "")[:48]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
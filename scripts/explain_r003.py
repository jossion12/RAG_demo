# -*- coding: utf-8 -*-
"""看 r003 里「原本通过、这轮变成 coverage_gap 0.00」的题到底答了什么。

阈值从 0.3 降到 0.1，召回条数从 2.9 涨到 6.5。如果这些题答不出来是因为
上下文被低分片段淹没（context dilution），答案应该跑偏到别的条款上；
如果是因为模型摆烂拒答，答案应该是「未找到相关信息」。两者要分开，
因为前者说明阈值不能再低，后者说明是别的问题。
"""
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))
import run_iteration as R                                              # noqa: E402


def rows_of(tag):
    summ = json.load(io.open(os.path.join(ROOT, "eval", "runs", tag, "summary.json"),
                             encoding="utf-8"))
    cases = {c["id"]: c for c in json.load(
        io.open(os.path.join(ROOT, summ["cases"]), encoding="utf-8"))["cases"]}
    out = {}
    for line in io.open(os.path.join(ROOT, "eval", "runs", tag, "raw.jsonl"), encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        c = cases.get(rec.get("id"))
        if c:
            out[c["id"]] = (R.rescore(c, rec)["row"], rec["res"])
    return out


def main():
    a = rows_of("r001_baseline")
    b = rows_of("r003_node010")
    regressed = [i for i in sorted(b) if a.get(i, (None,))[0]["passed"] and not b[i][0]["passed"]]
    fixed = [i for i in sorted(b) if not a.get(i, (None,))[0]["passed"] and b[i][0]["passed"]]

    print("这轮修好 %d 条: %s" % (len(fixed), " ".join(fixed)))
    print("这轮改坏 %d 条，逐条看答案:\n%s\n" % (len(regressed), " ".join(regressed)))
    for i in regressed:
        ra, rb = a[i][0], b[i][0]
        ans = (b[i][1].get("answer") or "").replace("\n", " ").strip()
        refused = "拒答" if not b[i][1].get("citations") else "有引用"
        print("─" * 92)
        print("%s  %s   基线引用 %d 条 -> 本轮引用 %d 条" %
              (i, rb["question"], ra.get("n_cites", 0), rb.get("n_cites", 0)))
        print("  归因 %s | %s | 覆盖 %.2f" % (rb["fail_class"], refused, rb.get("coverage") or 0))
        print("  答案: %s" % (ans[:300] or "(空)"))
        print("  引用文档: %s" % ("/".join(rb.get("cited_products") or []) or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
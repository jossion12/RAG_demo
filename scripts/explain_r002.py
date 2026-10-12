# -*- coding: utf-8 -*-
"""查矛盾：检索扫描说阈值 0.3->0.1 能把章节命中从 59% 提到 79%，
但全量评测 r002 几乎没有变好。到底是「改了没生效」还是「生效了但没用」。

判据：逐条比较两轮的引用条数与首条分数。
  - 引用条数没变  -> 数据库改了但运行时没吃进去（生效位置搞错了）
  - 引用条数变了但通过数没变 -> 生效了，问题出在阈值之外（LLM 侧被噪声干扰）
"""
import io
import json
import os
import statistics
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
            out[c["id"]] = R.rescore(c, rec)["row"]
    return out


def main():
    a = rows_of("r001_baseline")
    b = rows_of("r002_th010")
    common = sorted(set(a) & set(b))

    na = [a[i].get("n_cites", 0) for i in common]
    nb = [b[i].get("n_cites", 0) for i in common]
    print("两轮共 %d 条可比\n" % len(common))
    print("引用条数  r001(阈值0.3) 均值 %.2f 中位 %d | r002(阈值0.1) 均值 %.2f 中位 %d"
          % (statistics.mean(na), statistics.median(na),
             statistics.mean(nb), statistics.median(nb)))

    sa = [a[i].get("top_score") for i in common if a[i].get("top_score")]
    sb = [b[i].get("top_score") for i in common if b[i].get("top_score")]
    print("首条分数  r001 均值 %.4f | r002 均值 %.4f"
          % (statistics.mean(sa), statistics.mean(sb)))
    print("零引用条数 r001 %d 条 | r002 %d 条"
          % (sum(1 for x in na if x == 0), sum(1 for x in nb if x == 0)))
    print("平均引用数变化 %+.2f" % (statistics.mean(nb) - statistics.mean(na)))

    print("\n引用条数发生变化的用例:")
    changed = [(i, a[i].get("n_cites", 0), b[i].get("n_cites", 0),
                a[i]["passed"], b[i]["passed"]) for i in common
               if a[i].get("n_cites", 0) != b[i].get("n_cites", 0)]
    print("  共 %d 条" % len(changed))
    for i, x, y, pa, pb in changed[:20]:
        print("   %-6s 引用 %d -> %d   %s -> %s" % (i, x, y, "过" if pa else "不过",
                                                     "过" if pb else "不过"))

    print("\n判定:")
    if abs(statistics.mean(nb) - statistics.mean(na)) < 0.3:
        print("  引用条数几乎没变 -> 阈值改动**没有进入运行时**，生效位置不对。")
    else:
        print("  引用条数明显变化 -> 阈值已生效，但通过数没跟着涨，")
        print("  说明瓶颈不在阈值，继续往下调阈值不会有收益。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
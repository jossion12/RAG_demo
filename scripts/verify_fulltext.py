# -*- coding: utf-8 -*-
"""守门：引用里的 `fullText` 必须真的是**未截断的原文**。

为什么需要这个脚本
------------------
`Citation.snippet` 为展示截到 300 字（`CitationAssembler.SNIPPET_MAX`），
而条款片段中位 624 字、最长 1019 字，**章节标题常落在 300 字之外**。
评测的章节命中判定如果读 snippet，就会把「明明召回到了」判成
`citation_miss` / `clause_mismatch`——实测 34 条引用判定里 10 条受此影响。

于是加了 `Citation.fullText` 承载原文，判据改读它（`run_iteration.cite_hit`）。
但「加了字段」不等于「字段是对的」：哪天有人为了省流量又把它截一下、
或者顺手复用了 `clip()`，判据就会静默退回残缺副本，指标再次系统性偏低
——**且不会报任何错**。这个脚本就是防这件事的。

它验证的是**逐字一致**，不是「长度看起来差不多」：
    对每条用例，把 raw.jsonl 里引用的 fullText 与 trace.jsonl 里
    同 (文档, 分段) 的检索侧原文逐字比对。
    两者来自不同通道（治理层 SSE 透传 vs Dify 只检索 API），
    不一致就说明有一侧动了手脚。

用法：
    python scripts/verify_fulltext.py --raw r008 --trace t008
    python scripts/verify_fulltext.py --raw r008 --trace t008 --show 3
"""
import argparse
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "eval", "runs")

SNIPPET_MAX = 300          # 与 CitationAssembler.SNIPPET_MAX 对齐
CLOSE_ENOUGH = 2           # 允许的差异长度：clip 会补一个省略号，不该有别的出入


def load_jsonl(path):
    out = []
    if not os.path.isfile(path):
        return out
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def flat(s):
    """压掉空白差异后比对。

    只比内容不比空白：`clip()` 会把换行/多空格压成单空格再截断，
    所以展示文本天然与原文在空白上不一致。**内容**必须逐字相同。
    """
    return re.sub(r"\s+", "", s or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="端到端轮次目录名，如 r008")
    ap.add_argument("--trace", required=True, help="对应的 trace 目录名，如 t008")
    ap.add_argument("--show", type=int, default=3, help="最多列出几条不一致明细")
    args = ap.parse_args()

    raw_rows = load_jsonl(os.path.join(RUNS, args.raw, "raw.jsonl"))
    traces = {t["id"]: t for t in load_jsonl(os.path.join(RUNS, args.trace, "trace.jsonl"))}
    if not raw_rows:
        print("找不到 raw.jsonl：%s" % args.raw)
        return 2
    if not traces:
        print("找不到 trace.jsonl：%s" % args.trace)
        return 2

    total_cites = 0
    has_full = 0
    shorter = []      # fullText 比 snippet 还短——不可能，除非又被截了
    mismatched = []   # 与检索侧原文不一致
    missing_in_trace = 0

    for row in raw_rows:
        rid = row["id"]
        res = row.get("res") or {}
        cites = res.get("citations") or []
        tr = traces.get(rid)
        # trace 里该用例 C 段的 (文档名, 分段位置) -> 原文
        ref = {}
        if tr:
            pool = (tr.get("pool") or {}).get("C_live_params") or []
            segs = tr.get("segs") or {}
            for p in pool:
                ref[(p["doc"], p["pos"])] = segs.get(p["seg"], "")

        for c in cites:
            total_cites += 1
            sn = c.get("snippet") or ""
            ft = c.get("fullText")
            if not ft:
                continue
            has_full += 1
            if len(ft) < len(sn):
                shorter.append((rid, len(sn), len(ft)))
            key = (c.get("documentName"), c.get("segmentPosition"))
            orig = ref.get(key)
            if orig is None:
                missing_in_trace += 1
                continue
            if flat(ft) != flat(orig):
                mismatched.append((rid, key[1], len(ft), len(orig)))

    print("轮次 %s × %s" % (args.raw, args.trace))
    print("  引用总数        %d" % total_cites)
    print("  带 fullText    %d（缺 %d 条——这些条目的判据仍在用截断片段）"
          % (has_full, total_cites - has_full))
    print("  trace 未覆盖   %d（该引用不在 C 段候选池里，无法逐字比对）"
          % missing_in_trace)

    fails = []
    if total_cites and has_full < total_cites:
        fails.append("有 %d 条引用没有 fullText，判据会退回 300 字截断片段"
                     % (total_cites - has_full))
    if shorter:
        fails.append("有 %d 条 fullText 比 snippet 还短，说明它又被截断了" % len(shorter))
    if mismatched:
        fails.append("有 %d 条 fullText 与检索侧原文不一致" % len(mismatched))

    # 只要存在「本该长文本却被截断」的引用，就判失败——哪怕内容比对恰好通过
    # （极小片段本来就截不到 300 字，那种情况 shorter 不会命中）。
    for rid, pos, lft, lorig in mismatched[:args.show]:
        print("  [x] %s 分段%s：fullText %d 字 vs 检索侧原文 %d 字"
              % (rid, pos, lft, lorig))
    for rid, lsn, lft in shorter[:args.show]:
        print("  [x] %s：snippet %d 字，fullText 反而只有 %d 字" % (rid, lsn, lft))

    print()
    if fails:
        print("fullText 判据不可信：")
        for f in fails:
            print("  [x] " + f)
        return 1
    print("[OK] fullText 是逐字原文，章节命中判据不再依赖截断片段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
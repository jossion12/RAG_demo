# -*- coding: utf-8 -*-
"""离线核对：把 TOC_PAT 收窄后，r004 那 18 条「目录页噪声」里还剩几条。

为什么要离线重算而不是直接跑新评测：收窄判据会改变「目录页噪声」这个
指标的历史口径。如果直接跑 r005 再对比 r004，会把「判据变严」误读成
「噪声下降」。只有拿 r004 的原始引用（raw.jsonl）用新判据重算一遍，
才能分清哪些是真被治好了、哪些只是判据不再报了。

用法：
    python scripts/recheck_toc_on_r004.py
"""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RAW = os.path.join(ROOT, "eval", "runs", "r004_prompt", "raw.jsonl")

# 收窄后的判据：只认真正的目录/阅读指引标记，去掉「本公司」指代那一句。
# 那一句出现在**每份条款的正文首页**（「……简称……在本保险条款中，
# “本公司”指……」），是正文不是目录。留着它会让正文首页恒被判成噪声。
# 命中的原文见 docs/评测迭代报告.md §9.2。
TOC_PAT_STRICT = re.compile(r"阅\s*读\s*指\s*引|条\s*款\s*目\s*录")

# 收窄**前**的判据，原样留一份。
# 别改用 run_iteration.TOC_PAT 来当「旧判据」—— 它已经收窄过了，两边同源，
# 脚本会恒等输出 11/11，看着像跑通了其实什么也没比。第一版就踩了这个坑。
TOC_PAT_LEGACY = re.compile(
    r"阅\s*读\s*指\s*引|条\s*款\s*目\s*录|释\s*义|本保险条款中，“本公司”指")


def noisy_snippets(citations, pat, top=3):
    out = []
    for c in (citations or [])[:top]:
        flat = re.sub(r"\s+", "", c.get("snippet") or "")
        m = pat.search(flat)
        if m:
            out.append((m.group(0), flat[:110]))
    return out


def main():
    rows = []
    with open(RAW, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    print("r004 原始记录 %d 条\n" % len(rows))

    old_hits = new_hits = 0
    old_ids, new_ids = [], []
    detail = []
    for r in rows:
        # raw.jsonl 的结构是 {"id","question","res":{"citations":[...], ...}}，
        # 引用在 res 下面。第一版写成 r["citations"] 拿到空列表，输出 0/52，
        # 差点把「字段路径写错」当成「新判据一条都不命中」。
        cites = (r.get("res") or {}).get("citations") or []
        o = noisy_snippets(cites, TOC_PAT_LEGACY)
        n = noisy_snippets(cites, TOC_PAT_STRICT)
        if o:
            old_hits += 1
            old_ids.append(r.get("id"))
        if n:
            new_hits += 1
            new_ids.append(r.get("id"))
            detail.append((r.get("id"), n))

    print("旧判据（现行 TOC_PAT） 命中: %d/%d 条" % (old_hits, len(rows)))
    print("新判据（收窄）         命中: %d/%d 条" % (new_hits, len(rows)))
    print("\n被判据放过的（只是正文首页定义句，非目录）: %s"
          % [i for i in old_ids if i not in new_ids])
    print("\n新判据仍认定为真目录噪声的：")
    for rid, hits in detail:
        for m, frag in hits:
            print("  %-6s 命中 %-10r | %s" % (rid, m, frag))

    if not detail:
        print("\n结论：r004 的 18 条全部是正文首页定义句被误报，"
              "r005 的「目录页噪声」应直接看 0 —— 但仍以新评测实测为准。")


if __name__ == "__main__":
    main()

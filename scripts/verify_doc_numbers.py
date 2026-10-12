# -*- coding: utf-8 -*-
"""文档数字自证：README / docs 里写的评测指标，必须与 eval/runs/<tag>/summary.json 对得上。

文档里最常见的腐烂方式不是写错结论，而是**结论对、数字过期**——
评测跑完之后文档没跟着改，读者照着过期数字做判断。
这个脚本把 README 验收线表里的数字直接和存档比对，对不上就报错。

用法：
    python scripts/verify_doc_numbers.py --tag r003_node010
"""
import argparse
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# README「验收线与当前实测」表的行：(指标名, 验收线, summary.json 里的 summ 键)
TABLE = [
    ("应答类命中率", "answer"),
    ("拒答类正确率", "refuse"),
    ("跨产品题", "cross"),
    ("口语问法", "oral"),
    ("引用命中率", "cite"),
]

# 用例集扩到 52 条之后的分母；用例集变了这里要跟着改，否则提醒会一直响。
EXPECTED_DEN = {"answer": 42, "refuse": 10, "cross": 4, "oral": 28, "cite": 34}


def pct(num, den):
    return round(100.0 * num / den) if den else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="r003_node010")
    ap.add_argument("--doc", default="README.md")
    args = ap.parse_args()

    summ = json.load(io.open(os.path.join(ROOT, "eval", "runs", args.tag, "summary.json"),
                             encoding="utf-8"))["summ"]
    doc = io.open(os.path.join(ROOT, args.doc), encoding="utf-8").read()

    print("对账对象: %s 的 summary.json  vs  %s\n" % (args.tag, args.doc))
    ok = True
    for name, key in TABLE:
        num, den = summ[key]
        real = pct(num, den)
        row = next((l for l in doc.splitlines()
                    if l.startswith("| " + name)), None)
        if row is None:
            print("  ⚠ %-14s 文档里找不到这一行" % name)
            ok = False
            continue
        # 按单元格取，不能直接正则抓第一个百分号——表里第 2 列是「验收线」，
        # 第 3 列才是「实测」。抓错列会把 85% 当成实测值，对账永远失败。
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        measured = cells[2] if len(cells) > 2 else ""
        m = re.search(r"(\d+)%", measured)
        claimed = int(m.group(1)) if m else None
        mark = "✅" if claimed == real else "❌"
        if claimed != real:
            ok = False
        print("  %s %-14s 验收线 %-6s 实测列写 %-6s 存档 %d/%d = %d%%"
              % (mark, name, cells[1] if len(cells) > 1 else "?", measured, num, den, real))
        # 分母变了（比如用例集从 23 条扩到 52 条）也一并提示
        if claimed == real and den != EXPECTED_DEN.get(key):
            print("       ⚠ 分母为 %d，与历史基线不同，确认是有意为之" % den)

    print()
    print("结论: %s" % ("文档数字与存档一致" if ok else "**存在不一致，需要修文档**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
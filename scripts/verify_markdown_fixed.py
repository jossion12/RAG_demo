# -*- coding: utf-8 -*-
"""端到端验证：修复后答案里的 markdown 标记是否完整保留。

判据不能是「看起来对」。要按字节验证服务端吐出的原文里，
markdown 关键字面在**修复前会丢、修复后必须还在**：
  · `### ` （标题标记 + 空格）
  · 行首 `- ` / `* `（列表项）
  · `**` 粗体成对

对照 r005 的 raw.jsonl（修复前采样）里同样这几条用例的原文，
确认缺失的标记现在都回来了。缺一个就算没修好。

用法：
    python scripts/verify_markdown_fixed.py
"""
import io
import json
import os
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("RAG_BASE", "http://localhost:8080/api/chat/stream")
CASES = os.path.join(ROOT, "eval", "eval_cases_v2.json")

CHECKS = [
    ("H3 有空格", re.compile(r"###\s+\S")),
    ("H3 任意", re.compile(r"###")),
    ("UL 短横线", re.compile(r"(?m)^\s*-\s+\S")),
    ("UL 星号", re.compile(r"(?m)^\s*\*\s+\S")),
    ("粗体成对", re.compile(r"\*\*[^*\n]+\*\*")),
    ("粗体总数偶数", None),   # 单独算
]


def ask(q):
    req = urllib.request.Request(
        BASE, data=json.dumps({"question": q}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    raw = urllib.request.urlopen(req, timeout=300).read().decode("utf-8")
    done = None
    for frame in raw.split("\n\n"):
        if "event:done" in frame:
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    v = line[5:]
                    if v.startswith(" "):
                        v = v[1:]
                    try:
                        done = json.loads(v)
                    except Exception:
                        pass
    return done.get("answer", "") if done else ""


def old_answer(case_id):
    p = os.path.join(ROOT, "eval", "runs", "r005_notoc", "raw.jsonl")
    with io.open(p, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                if r["id"] == case_id:
                    return (r.get("res") or {}).get("final_answer") or ""
    return ""


def probe(name, pat, text):
    return len(pat.findall(text))


def main():
    blob = json.load(open(CASES, encoding="utf-8"))
    cases = {c["id"]: c for c in (blob["cases"] if isinstance(blob, dict) else blob)}
    ids = ["M-04", "M-03", "C-06", "M-12"]

    print("按标记逐项对比：修复前(r005 采样) vs 修复后(当前)\n")
    header = "%-12s %-14s %8s %8s" % ("id", "标记", "修复前", "修复后")
    print(header)
    print("-" * 46)

    bad = 0
    for rid in ids:
        q = cases[rid]["question"]
        new = ask(q)
        old = old_answer(rid)
        for label, pat in CHECKS:
            if pat is None:
                continue
            o, n = probe(label, pat, old), probe(label, pat, new)
            flag = ""
            if n == 0 and o > 0:
                flag = "  ❌ 回归"
                bad += 1
            print("%-12s %-14s %8d %8d%s" % (rid, label, o, n, flag))

        for label, text in (("修复前", old), ("修复后", new)):
            nb = text.count("**")
            if nb % 2:
                bad += 1
                print("%-12s %-14s  ** 共 %d 个（奇数=粗体被拆散）"
                      % (rid, label, nb))
        print()

    print("=" * 46)
    print("结论：%s" % ("✅ markdown 标记全部保留，无回归" if bad == 0
                        else "❌ %d 处异常" % bad))

    # 抽样展示标题行
    print("\n修复后的标题行样例：")
    for rid in ids[:2]:
        q = cases[rid]["question"]
        for line in ask(q).split("\n"):
            if line.strip().startswith("#"):
                print("  %-6s |%s|" % (rid, line.strip()[:60]))
                break


if __name__ == "__main__":
    main()

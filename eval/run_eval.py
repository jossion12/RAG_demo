# -*- coding: utf-8 -*-
"""离线评测（FR-09 / AC-02 / AC-04 / AC-07 / AC-09 / AC-10）。

批量跑评测集并输出报告。判分口径写在这里，便于复现：
  覆盖率 = 命中的 expected_answer_points 数 / 总数
  应答类：覆盖率 >= 0.5 记为 answer_hit，且必须未拒答
  拒答类：系统明确拒答记为 refuse_hit
  引用命中：任一 citation 的 label/snippet 含 expected_clause

用法：
    & "C:\\Python312\\python.exe" eval\\run_eval.py --base http://localhost:8080
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "eval", "eval_cases.json")
REPORT = os.path.join(ROOT, "eval", "eval-report.md")

REFUSE_MARKERS = ["未找到相关信息", "未找到", "无法回答", "没有找到", "抱歉"]


def post_json(url, payload, timeout=180):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def coverage(answer, points):
    """按关键信息点命中数算覆盖率。期望点必须是答案里应当出现的具体字样。"""
    if not points:
        return 1.0, []
    hit = [p for p in points if p and p.replace(" ", "") in (answer or "").replace(" ", "")]
    return len(hit) / len(points), hit


def is_refused(answer, citations):
    if not citations:
        return True
    a = answer or ""
    return any(m in a for m in REFUSE_MARKERS)


def call_sse(base, question):
    """走 SSE 端点，逐事件收集 delta / citations / done。"""
    url = base.rstrip("/") + "/api/chat/stream"
    data = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    answer, citations, done = [], [], {}
    with urllib.request.urlopen(req, timeout=300) as r:
        event = None
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
                if event == "delta":
                    answer.append(payload)
                elif event == "citations":
                    try:
                        citations = json.loads(payload)
                    except Exception:
                        pass
                elif event in ("done", "blocked"):
                    try:
                        done = json.loads(payload)
                    except Exception:
                        done = {}
                    if event == "blocked":
                        done["blocked"] = True
    return {
        "answer": "".join(answer),
        "citations": citations,
        "refused": bool(done.get("refused")) or not citations,
        "elapsed_ms": done.get("elapsedMs"),
    }


def call_blocking(base, question):
    url = base.rstrip("/") + "/api/chat"
    res = post_json(url, {"question": question})
    return {
        "answer": res.get("answer", ""),
        "citations": res.get("citations", []),
        "refused": res.get("refused", False),
        "elapsed_ms": res.get("elapsedMs"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--mode", choices=["sse", "blocking"], default="sse")
    ap.add_argument("--out", default=REPORT)
    args = ap.parse_args()

    with io.open(CASES, encoding="utf-8") as f:
        suite = json.load(f)
    cases = suite["cases"]

    rows = []
    print("评测 %d 条  (mode=%s  base=%s)" % (len(cases), args.mode, args.base))
    for i, c in enumerate(cases, 1):
        q = c["question"]
        t0 = time.time()
        try:
            res = call_sse(args.base, q) if args.mode == "sse" else call_blocking(args.base, q)
            err = None
        except urllib.error.HTTPError as e:
            res, err = {"answer": "", "citations": [], "refused": False}, "HTTP %s %s" % (e.code, e.reason)
        except Exception as e:                                   # noqa: BLE001
            res, err = {"answer": "", "citations": [], "refused": False}, str(e)[:120]
        dt = int((time.time() - t0) * 1000)

        cov, hit_points = coverage(res.get("answer", ""), c.get("expected_answer_points", []))
        refused = res.get("refused", False)
        clause = c.get("expected_clause")
        cite_hit = False
        if clause:
            blob = json.dumps(res.get("citations", []), ensure_ascii=False)
            cite_hit = clause.replace(" ", "") in blob.replace(" ", "")

        if c.get("expect_refuse"):
            passed = refused
            kind = "refuse"
        else:
            passed = (not refused) and cov >= 0.5 and (cite_hit if clause else True)
            kind = "answer"

        rows.append(dict(id=c["id"], product=c["product"], category=c.get("category", ""),
                         question=q, kind=kind, passed=passed, refused=refused,
                         coverage=round(cov, 3), hit_points=hit_points,
                         clause=clause, cite_hit=cite_hit,
                         elapsed_ms=res.get("elapsed_ms") or dt,
                         answer=(res.get("answer") or "")[:160], error=err))
        flag = "OK " if passed else "FAIL"
        print("  [%s] %-5s %-12s cov=%.2f refuse=%-5s %6dms  %s"
              % (flag, c["id"], kind, cov, str(refused), rows[-1]["elapsed_ms"], q[:34]))

    # 汇总
    def rate(pred):
        sel = [r for r in rows if pred(r)]
        return (sum(1 for r in sel if r["passed"]), len(sel))

    ans_ok, ans_n = rate(lambda r: r["kind"] == "answer")
    ref_ok, ref_n = rate(lambda r: r["kind"] == "refuse")
    cross = [r for r in rows if r["category"] == "cross"]
    cross_ok = sum(1 for r in cross if r["passed"])
    oral = [r for r in rows if r["category"] == "oral" and r["kind"] == "answer"]
    oral_ok = sum(1 for r in oral if r["passed"])
    cite_rows = [r for r in rows if r["clause"]]
    cite_ok = sum(1 for r in cite_rows if r["cite_hit"])

    md = []
    md.append("# 评测报告\n")
    md.append("- 数据集：`eval/eval_cases.json` (%d 条)\n" % len(cases))
    md.append("- 目标：`%s`\n" % args.base)
    md.append("- 模式：%s\n" % args.mode)
    md.append("\n## 总览\n")
    md.append("| 指标 | 值 | 验收线 | 结论 |")
    md.append("| --- | --- | --- | --- |")
    def line(name, num, den, bar):
        pct = (num / den) if den else 0.0
        return "| %s | %d/%d = %.0f%% | %s | %s |" % (name, num, den, pct * 100, bar,
                                                        "✅" if pct >= 0.85 else "❌")
    md.append(line("应答类命中率 (AC-02)", ans_ok, ans_n, "≥85%"))
    md.append(line("拒答类正确率 (AC-04)", ref_ok, ref_n, "≥85%"))
    md.append(line("跨产品题 (AC-09)", cross_ok, len(cross), "≥90%"))
    md.append(line("口语问法 (AC-10)", oral_ok, len(oral), "≥75%"))
    md.append(line("引用命中率 (AC-03)", cite_ok, len(cite_rows), "≥90%"))
    md.append("\n## 明细\n")
    md.append("| # | id | 类别 | 问题 | 覆盖 | 拒答 | 引用 | 耗时 | 结论 |")
    md.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        md.append("| %d | %s | %s | %s | %.2f | %s | %s | %dms | %s |"
                  % (rows.index(r) + 1, r["id"], r["category"], r["question"],
                     r["coverage"], "是" if r["refused"] else "否",
                     ("命中" if r["cite_hit"] else "未中") if r["clause"] else "-",
                     r["elapsed_ms"], "✅" if r["passed"] else "❌"))
    md.append("\n## 失败明细\n")
    fails = [r for r in rows if not r["passed"]]
    if not fails:
        md.append("无。\n")
    for r in fails:
        md.append("### %s %s\n" % (r["id"], r["question"]))
        md.append("- 期望：%s，覆盖率 %.2f%s" % (
            "拒答" if r["kind"] == "refuse" else "应答且覆盖 %s" % (r["clause"] or "要点"),
            r["coverage"], "，引用命中" if r["cite_hit"] else "，引用未命中"))
        md.append("- 实际：%s" % (r["error"] or ("拒答" if r["refused"] else r["answer"][:140])))
        md.append("")

    with io.open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("\n报告: %s" % args.out)
    print("应答 %d/%d | 拒答 %d/%d | 跨产品 %d/%d | 口语 %d/%d"
          % (ans_ok, ans_n, ref_ok, ref_n, cross_ok, len(cross), oral_ok, len(oral)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
# -*- coding: utf-8 -*-
"""闭环评测器：走前端链路取数 → 原始结果逐条落盘 → 失败归因 → 跨轮对比。

为什么不是复用 run_eval.py：
    run_eval.py 只输出「过/不过」。调参需要回答的是「**为什么**不过」——
    是阈值砍掉了正确段（over_refusal）、是串了产品（wrong_product）、
    还是答案对了但引用没兜住（citation_miss）。这三类要用不同的旋钮去解，
    混在一个 passed=False 里就只能靠猜。本脚本把失败**归因**并按危险度排序，
    同时把每次调参前后的每条结果都留在磁盘上，可回溯、可复现。

取数口径与前端完全一致：
    POST {base}/api/chat/stream  {"question": ...}
    SSE 分帧 -> event:delta|citations|done|blocked|error
    data 多行以 \n 连接（与 frontend/src/App.jsx:53-55 同一套解析）

⚠️ delta 必须 JSON.parse —— 2026-10-13 修正。
    后端把 delta 改成 JSON 字符串承载（ChatService#sendDelta）之后，
    本脚本曾一直按裸文本原样拼接。两者一度同源，但前端改了、评测没改，
    于是同一份响应在页面上显示正常、在评测里却变成
    "除""另""有""约""外" 这种逐字转义的文本（数字还被劈成 3" "0），
    答案覆盖率全线归零、应答类命中率从 38% 掉到 7%。
    **这类不同步不会报错，只会静默产出错误结论**，改任一侧都要同步另一侧。

判分与失败归因：
    先判是否拦截/异常，再按期望类型分流。归因优先级从高到低：
        error            请求异常（HTTP/超时/解析失败）
        blocked          入参过滤拦截（合规红线）
        wrong_product    串档：引用里全是别的产品文档 —— 客户拿到错误产品的条款
        over_refusal     误拒：该答却拒答/无引用
        under_refusal    误答：该拒却给了引用答案
        citation_miss    答了但引用没兜住期望条款（coverage 高时=无据之答）
        coverage_gap     引用对、答了，但关键信息点缺失

用法：
    python eval/run_iteration.py --tag r001_baseline
    python eval/run_iteration.py --tag r002_th04 --cases eval/eval_cases_v2.json
    python eval/run_iteration.py --compare r001_baseline r002_th04
    python eval/run_iteration.py --list-runs
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
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "eval", "runs")
DEFAULT_CASES = os.path.join(ROOT, "eval", "eval_cases_v2.json")

# 引用里的 documentName -> 产品代号。串档判定完全依赖这张表，
# 新增文档时必须同步维护，否则 wrong_product 永远判不出来。
DOC_PRODUCT = {
    "太保互联网个人一年期住院医疗保险条款": "med_ind",
    "太保团体百万医疗保险互联网版条款": "med_group",
    "太保团体终身重大疾病保险E款条款": "ci",
}

# 用例声明的产品 -> 允许出现在引用里的产品集合。
# 「medical」含两份医疗文档：这两份条款章节号不同（个人 2.4 免赔额 / 团体 2.5 免赔额），
# 只按「医疗 vs 重疾」二分会把文档级串档漏掉，所以保留到文档粒度。
CASE_PRODUCTS = {
    "med_ind": {"med_ind"},
    "med_group": {"med_group"},
    "medical": {"med_ind", "med_group"},
    "critical": {"ci"},
    "both": {"med_ind", "med_group", "ci"},
    "none": set(),
}

REFUSE_MARKERS = ["未找到相关信息", "未找到", "无法回答", "没有找到", "抱歉", "暂无相关信息", "不予回答"]

# 失败归因 -> 危险度。危 3 = 可能让客户拿到错误结论，必须优先修；
# 危 2 = 用户白跑一趟；危 1 = 答案不全但方向对；危 0 = 环境问题。
SEVERITY = {
    "wrong_product": 3,
    "clause_mismatch": 3,
    "under_refusal": 3,
    "citation_miss": 3,
    "blocked": 2,
    "over_refusal": 2,
    "coverage_gap": 1,
    "error": 0,
}
SEV_LABEL = {3: "危", 2: "重", 1: "轻", 0: "环"}


# --------------------------------------------------------------------------
# 取数：与 frontend/src/App.jsx 同一套 SSE 解析
# --------------------------------------------------------------------------
def call_frontend_sse(base, question, timeout=300):
    """POST /api/chat/stream，逐帧解析，返回与前端渲染一致的结果。"""
    url = base.rstrip("/") + "/api/chat/stream"
    data = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")

    answer_parts, citations, done, blocked = [], [], {}, {"blocked": False, "hits": []}
    events, err = [], None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # 按字节喂 TextDecoder 流，等价于前端 decoder.decode(value, {stream:true})
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        while True:
            chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
            if not chunk:
                break
            buffer += dec.decode(chunk)
            while True:
                idx = buffer.find("\n\n")
                if idx == -1:
                    break
                frame, buffer = buffer[:idx], buffer[idx + 2:]
                event = ""
                data_lines = []
                for line in frame.split("\n"):
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:][1:] if line[5:].startswith(" ") else line[5:])
                if not data_lines:
                    continue
                payload = "\n".join(data_lines)      # 前端 dataLines.join('\n')
                events.append(event)
                if event == "delta":
                    # 与 frontend/src/App.jsx:35-39 同一套解析：delta 的 payload
                    # 是**JSON 字符串**（后端 ChatService#sendDelta 用 json(text)
                    # 承载，避免 SSE 分隔空格吃掉以空格开头的帧）。
                    # 直接原样拼接会得到 "除""另""有""约" 这种逐字被转义的文本
                    # （数字还会被劈成 3" "0），答案本身就是坏的，覆盖率会全线归零。
                    # 这里必须解出来；解析失败再退回裸文本，兼容未升级的后端。
                    try:
                        v = json.loads(payload)
                        answer_parts.append(v if isinstance(v, str) else payload)
                    except Exception:
                        answer_parts.append(payload)
                elif event == "citations":
                    try:
                        citations = json.loads(payload)
                    except Exception:
                        pass
                elif event in ("done", "blocked"):
                    try:
                        d = json.loads(payload)
                    except Exception:
                        d = {}
                    if event == "blocked":
                        blocked = {"blocked": True, "hits": d.get("hits", [])}
                    else:
                        done = d
                elif event == "error":
                    try:
                        err = json.loads(payload).get("message", payload)
                    except Exception:
                        err = payload

    answer = "".join(answer_parts)
    # 前端 done 里带的是完整 answer；delta 拼接结果与它应当一致，
    # 不一致说明流式分帧把多行文本拆坏了 —— 本身就是一条要报告的缺陷。
    final_answer = done.get("answer", answer)
    return {
        "answer": answer,
        "final_answer": final_answer,
        "citations": citations or [],
        "refused": bool(done.get("refused")),
        "blocked": blocked,
        "elapsed_ms": done.get("elapsedMs"),
        "events": events,
        "error": err,
        "delta_matches_done": (answer.strip() == (final_answer or "").strip()),
    }


# --------------------------------------------------------------------------
# 判分与归因
# --------------------------------------------------------------------------
def norm(s):
    return re.sub(r"\s+", "", s or "")


def coverage(answer, points):
    if not points:
        return 1.0, []
    blob = norm(answer)
    hit = [p for p in points if norm(p) in blob]
    return len(hit) / float(len(points)), hit


def cited_products(citations):
    out = set()
    for c in citations or []:
        name = c.get("documentName") or ""
        for doc, prod in DOC_PRODUCT.items():
            if doc in name:
                out.add(prod)
    return out


def cite_hit(citations, clause):
    """引用命中判定，分三档而不是一个布尔值。

    为什么要分档：章节号和标题在不同文档里会重名——个人医疗的「2.7 责任免除」
    和重疾E 的「2.4 责任免除」标题一样、编号不同。只看标题会把串档当成命中；
    只看编号会到处误命中（正文里「2.4」出现频率极高）。所以：
      exact      -> 编号和标题在同一条引用里都对上
      title_only -> 只有标题对上了（内容方向对，但很可能来自另一份文档的同名章节）
      False      -> 完全没召回
    title_only 在归因里单列为 clause_mismatch：它比 wrong_product 隐蔽，
    因为答案读起来完全合理，但引用兜不住用户问的那一份文档。

    ⚠️ 2026-10-13 修正：优先用 `fullText`（未截断原文）判定。
        治理层的 `snippet` 为展示截到 300 字，而条款片段中位 624 字、最长 1019 字，
        **章节标题常落在 300 字之外**，用 snippet 判会把「明明召回到了」
        判成 citation_miss / clause_mismatch——实测 34 条判定里 10 条受此影响，
        导致引用命中率指标系统性偏低。
        判据拿展示副本做判断，等于强迫它在残缺文本上工作。
        历史轮次没有 fullText，退回 snippet 即可（旧数据的结论保持原样）。
    """
    if not clause:
        return None
    m = re.match(r"^\s*([\d.]+)\s+(.*)$", clause.strip())
    num, title = (m.group(1), m.group(2).strip()) if m else (None, clause.strip())
    title_only = False
    for c in citations or []:
        # 完整原文优先；老数据没有这个字段时退回展示片段
        body = c.get("fullText") or c.get("snippet") or ""
        blob = norm((c.get("label") or "") + body)
        if norm(title) and norm(title) in blob:
            title_only = True
            if num and norm(num) in blob:
                return "exact"
    return "title_only" if title_only else False


# 知识库里混进了目录/阅读指引页，它们对「犹豫期」这类词面命中极高，
# rerank 会把正文条款挤出候选。这几页不是有效召回，单独记一个诊断指标。
#
# 只认真正的目录标记。曾经这里还有一支「本保险条款中，“本公司”指」，
# 那是错的：那句在**每份条款的正文首页**都有（「……简称……在本保险条款中，
# “本公司”指……」），是正文不是目录，留在判据里会让正文首页恒被判成噪声。
# 证据：scripts/recheck_toc_on_r004.py 用 r004 的原始引用（raw.jsonl）重算，
#       旧判据 18 条 -> 收窄后 11 条，放过的 7 条全是正文首页的定义句
#       （其中 2 条是「14.1 父母」这类真实定义内容）。
#       命中的原文见 docs/评测迭代报告.md §9.2。
TOC_PAT = re.compile(r"阅\s*读\s*指\s*引|条\s*款\s*目\s*录")


def toc_noise(citations, top=3):
    hits = 0
    for c in (citations or [])[:top]:
        # 与 cite_hit 同一口径：能用完整原文就别用展示片段，
        # 免得两个指标读的不是同一份文本（判据之间本身也会打架）。
        body = c.get("fullText") or c.get("snippet") or ""
        if TOC_PAT.search(re.sub(r"\s+", "", body)):
            hits += 1
    return hits


def classify(case, res):
    """返回 (passed, failure_class, detail)。"""
    if res.get("error"):
        return False, "error", {"err": str(res["error"])[:200]}

    answer = res.get("answer") or ""
    cites = res.get("citations") or []
    # 拒答判定要同时看三个信号，缺一个都会误判：
    #   1) Dify 自己的 refused 标记（检索节点一条都没召回时才为真）
    #   2) 有没有引用（治理层把空引用也算拒答）
    #   3) 正文里是否说了拒答话术 —— 阈值调低后会出现「有引用但正文答的是
    #      『未找到相关信息』」，只��前两个信号会把这种答对了的情况判成误答。
    blob = norm(answer)
    text_refused = any(norm(m) in blob for m in REFUSE_MARKERS)
    refused = bool(res.get("refused")) or not cites or text_refused
    expected_points = case.get("expected_answer_points", [])
    clause = case.get("expected_clause")
    cov, hit_points = coverage(answer, expected_points)
    chit = cite_hit(cites, clause)

    info = {"coverage": round(cov, 3), "hit_points": hit_points,
            "cite_hit": chit, "cites": len(cites), "toc_noise": toc_noise(cites)}
    prods = cited_products(cites)
    info["cited_products"] = sorted(prods)

    if case.get("expect_refuse"):
        if refused:
            return True, "pass", info
        info["why"] = "期望拒答，却返回了 %d 条引用" % len(cites)
        return False, "under_refusal", info

    allow = CASE_PRODUCTS.get(case.get("product"), set())
    # 串档优先于覆盖判定：产品错了，覆盖率再高也是错的答案。
    if allow and prods and not (prods & allow):
        info["why"] = "引用只出现 %s，期望 %s" % (sorted(prods), sorted(allow))
        return False, "wrong_product", info
    if chit == "title_only":
        where = "；".join(
            "%s#%s" % (c.get("documentName", "")[:10], re.sub(r"\s+", "", (c.get("snippet") or "")[:26]))
            for c in cites if norm(re.sub(r"^[\d.]+\s*", "", clause or "").strip()) in norm(c.get("snippet") or ""))
        info["why"] = "召回了同名章节但编号/文档不对（期望「%s」），命中位置：%s" % (clause, where)
        return False, "clause_mismatch", info
    if refused:
        info["why"] = "期望应答，但被拒答或无引用"
        return False, "over_refusal", info
    if clause and chit is False:
        info["why"] = "答案覆盖 %.2f，但引用里没有「%s」相关内容" % (cov, clause)
        return False, "citation_miss", info
    if cov < 0.5:
        missing = [p for p in expected_points if p not in hit_points]
        info["why"] = "缺少要点 %s" % missing
        return False, "coverage_gap", info
    return True, "pass", info


# --------------------------------------------------------------------------
# 单轮执行
# --------------------------------------------------------------------------
def run_one(base, case, mode):
    q = case["question"]
    t0 = time.time()
    err = None
    try:
        if mode == "sse":
            res = call_frontend_sse(base, q)
        else:
            data = json.dumps({"question": q}).encode("utf-8")
            req = urllib.request.Request(
                base.rstrip("/") + "/api/chat", data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=300) as r:
                b = json.loads(r.read().decode("utf-8"))
            res = {"answer": b.get("answer", ""), "final_answer": b.get("answer", ""),
                   "citations": b.get("citations", []), "refused": b.get("refused", False),
                   "blocked": {"blocked": bool(b.get("blockedBy")), "hits": b.get("blockedBy", [])},
                   "elapsed_ms": b.get("elapsedMs"), "events": ["done"],
                   "error": None, "delta_matches_done": True}
    except urllib.error.HTTPError as e:
        res, err = {"error": "HTTP %s %s" % (e.code, e.reason)}, "http"
    except Exception as e:                                    # noqa: BLE001
        res, err = {"error": "%s: %s" % (type(e).__name__, str(e)[:150])}, "exc"
    wall = int((time.time() - t0) * 1000)

    if err:
        case_res = {"id": case["id"], "question": q, "transport_error": err,
                    "raw": res, "wall_ms": wall}
        return {"case": case, "row": {"id": case["id"], "question": q, "passed": False,
                                       "fail_class": "error", "wall_ms": wall,
                                       "info": {"err": str(res.get("error"))[:200]}},
                "raw": case_res}

    return build_row(case, res, wall)


def build_row(case, res, wall):
    """把一次响应打成结果行。取数与判分在这里分开，
    断点续跑时可以拿磁盘上已存的响应重新判分而**不重新发请求**。"""
    q = case["question"]
    passed, cls, info = classify(case, res)
    if res.get("blocked", {}).get("blocked"):
        # 合规类用例的期望就是「拦下」，被拦等于做对了；只有把该答的题拦了才是缺陷。
        if case.get("expect_refuse"):
            passed, cls = True, "pass"
            info["why"] = "入参过滤拦截（符合期望）hits=%s" % res["blocked"].get("hits")
        else:
            passed, cls = False, "blocked"
            info["why"] = "入参过滤拦截 hits=%s" % res["blocked"].get("hits")

    row = {
        "id": case["id"], "product": case.get("product"), "category": case.get("category", ""),
        "question": q, "kind": "refuse" if case.get("expect_refuse") else "answer",
        "passed": passed, "fail_class": cls,
        "refused": bool(res.get("refused")) or not res.get("citations"),
        "coverage": info.get("coverage"), "cite_hit": info.get("cite_hit"),
        "cited_products": info.get("cited_products", []),
        "toc_noise": info.get("toc_noise", 0),
        "n_cites": len(res.get("citations") or []),
        "top_score": (res["citations"][0].get("score") if res.get("citations") else None),
        "elapsed_ms": res.get("elapsed_ms") or wall, "wall_ms": wall,
        "why": info.get("why", ""),
        "answer": (res.get("answer") or "")[:400],
        "delta_mismatch": not res.get("delta_matches_done", True),
    }
    return {"case": case, "row": row, "raw": {"id": case["id"], "question": q, "res": res}}


def rescore(case, stored):
    """用磁盘上存下的响应重新判分（续跑路径，不触网）。"""
    res = stored.get("res")
    if not res:                      # 早期版本或传输失败记录
        res = stored.get("raw") or {"error": "no stored response"}
        return {"case": case,
                "row": {"id": case["id"], "question": case["question"], "passed": False,
                        "fail_class": "error", "wall_ms": stored.get("wall_ms", 0),
                        "info": {"err": str(res.get("error"))[:200]}},
                "raw": stored}
    return build_row(case, res, stored.get("wall_ms", 0))


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def summarize(rows):
    s = {}

    def rate(pred):
        sel = [r for r in rows if pred(r)]
        return sum(1 for r in sel if r["passed"]), len(sel)

    s["answer"] = rate(lambda r: r["kind"] == "answer")
    s["refuse"] = rate(lambda r: r["kind"] == "refuse")
    cross = [r for r in rows if r["category"] == "cross"]
    s["cross"] = (sum(1 for r in cross if r["passed"]), len(cross))
    oral = [r for r in rows if r["category"] == "oral" and r["kind"] == "answer"]
    s["oral"] = (sum(1 for r in oral if r["passed"]), len(oral))
    withclause = [r for r in rows if r.get("cite_hit") is not None]
    s["cite"] = (sum(1 for r in withclause if r["cite_hit"] == "exact"), len(withclause))

    ans_rows = [r for r in rows if r["kind"] == "answer"]
    s["wrong_product"] = (sum(1 for r in ans_rows if r["fail_class"] == "wrong_product"), len(ans_rows))
    s["clause_mismatch"] = (sum(1 for r in ans_rows if r["fail_class"] == "clause_mismatch"), len(ans_rows))
    s["over_refusal"] = (sum(1 for r in ans_rows if r["fail_class"] == "over_refusal"), len(ans_rows))
    s["unsupported"] = (sum(1 for r in ans_rows if r["fail_class"] == "citation_miss"), len(ans_rows))
    lat = sorted(r["elapsed_ms"] for r in rows if r.get("elapsed_ms"))
    s["p50"] = lat[len(lat) // 2] if lat else 0
    s["p95"] = lat[int(len(lat) * 0.95)] if lat else 0
    s["delta_mismatch"] = sum(1 for r in rows if r.get("delta_mismatch"))
    s["toc_noise"] = (sum(1 for r in rows if (r.get("toc_noise") or 0) > 0), len(rows))
    return s


def fmt_pct(num, den):
    return "%.0f%%" % (100.0 * num / den) if den else "-"


def write_report(tag, rows, summ, params, cases_path, mode, base, elapsed):
    md = []
    md.append("# 迭代评测报告 · %s\n" % tag)
    md.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    md.append("- 用例集：`%s`（%d 条）" % (os.path.basename(cases_path), len(rows)))
    md.append("- 取数链路：`%s/api/chat/%s`（前端同款）" % (base, "stream" if mode == "sse" else ""))
    md.append("- 总耗时：%.1f s\n" % elapsed)

    md.append("\n## 参数快照\n\n```json\n%s\n```\n" % json.dumps(params, ensure_ascii=False, indent=2))

    md.append("\n## 验收线\n")
    md.append("| 指标 | 值 | 验收线 | 结论 |")
    md.append("| --- | --- | --- | --- |")

    def line(name, key, bar):
        ok, n = summ[key]
        return "| %s | %d/%d = %s | %s | %s |" % (name, ok, n, fmt_pct(ok, n), bar,
                                                  "✅" if (n and ok / n >= bar) else "❌")
    md.append(line("应答类命中率 (AC-02)", "answer", 0.85))
    md.append(line("拒答类正确率 (AC-04)", "refuse", 0.85))
    md.append(line("跨产品题 (AC-09)", "cross", 0.90))
    md.append(line("口语问法 (AC-10)", "oral", 0.75))
    md.append(line("引用命中率 (AC-03)", "cite", 0.90))

    md.append("\n## 调参指标\n")
    md.append("| 指标 | 值 | 说明 |")
    md.append("| --- | --- | --- |")
    for name, key, note in [
        ("串档率", "wrong_product", "应答类里引用只有错误产品文档的比例——保险场景最危险"),
        ("同名章节错配", "clause_mismatch", "召回了正确话题，但来自另一份文档的同名章节（编号对不上）"),
        ("误拒率", "over_refusal", "应答类里被拒答的比例——用户白跑一趟"),
        ("无据之答", "unsupported", "答了但引用没兜住期望条款"),
    ]:
        ok, n = summ[key]
        md.append("| %s | %d/%d = %s | %s |" % (name, ok, n, fmt_pct(ok, n), note))
    ok, n = summ["toc_noise"]
    md.append("| ⚠ 目录页噪声 | %d/%d = %s | 前 3 条引用里出现「阅读指引/条款目录/释义」页——"
              "这些页对关键词命中极高，会把正文条款挤出候选 |" % (ok, n, fmt_pct(ok, n)))
    md.append("| P50 耗时 | %d ms | |" % summ["p50"])
    md.append("| P95 耗时 | %d ms | |" % summ["p95"])
    if summ["delta_mismatch"]:
        md.append("| ⚠ 流式分帧不一致 | %d 条 | delta 拼接结果与 done.answer 不符 |" % summ["delta_mismatch"])

    md.append("\n## 失败聚类\n")
    fails = [r for r in rows if not r["passed"]]
    if not fails:
        md.append("全部通过。\n")
    else:
        fails.sort(key=lambda r: (-SEVERITY.get(r["fail_class"], 0), r["id"]))
        md.append("| 危险度 | 归因 | 条数 | 用例 |")
        md.append("| --- | --- | --- | --- |")
        by = {}
        for r in fails:
            by.setdefault(r["fail_class"], []).append(r["id"])
        for cls, ids in sorted(by.items(), key=lambda kv: -SEVERITY.get(kv[0], 0)):
            md.append("| %s | `%s` | %d | %s |" % (SEV_LABEL.get(SEVERITY.get(cls, 0), "-"),
                                                   cls, len(ids), " ".join(ids)))

    md.append("\n## 检索画像\n")
    md.append("每题引用里出现的文档产品与首条分数，用来定位是召回问题还是阈值问题。\n")
    md.append("| id | 问题 | 首条分 | 引用数 | 目录噪声 | 引用文档产品 | 结论 |")
    md.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        md.append("| %s | %s | %s | %d | %s | %s | %s |" % (
            r["id"], r["question"], r.get("top_score"), r.get("n_cites", 0),
            r.get("toc_noise", 0) or "-",
            "/".join(r.get("cited_products") or []) or "-",
            "✅" if r["passed"] else "❌ %s" % r["fail_class"]))

    md.append("\n## 失败明细\n")
    if not fails:
        md.append("无。\n")
    for r in fails:
        md.append("### [%s] `%s` %s\n" % (SEV_LABEL.get(SEVERITY.get(r["fail_class"], 0), "-"),
                                         r["fail_class"], r["question"]))
        md.append("- 期望：%s" % ("拒答" if r["kind"] == "refuse"
                                else "应答，覆盖 %s / 引用 %s"
                                % (r.get("coverage"), r.get("cite_hit"))))
        md.append("- 归因：%s" % (r.get("why") or "-"))
        md.append("- 引用文档：%s（%d 条）" % ("/".join(r.get("cited_products") or []) or "无", r.get("n_cites", 0)))
        md.append("- 答案：%s" % ((r.get("answer") or "").replace("\n", " ")[:260] or "(空)"))
        md.append("")

    return "\n".join(md)


# --------------------------------------------------------------------------
# 跨轮对比
# --------------------------------------------------------------------------
def do_compare(a_tag, b_tag):
    def load(tag):
        d = os.path.join(RUNS, tag)
        summ = json.load(io.open(os.path.join(d, "summary.json"), encoding="utf-8"))
        # raw.jsonl 里存的是原始响应，没有 passed 字段 —— 必须按用例集重新判分。
        # 两轮用例集可能不同，所以用各自 summary 里记的那一份。
        cpath = summ.get("cases") or "eval/eval_cases_v2.json"
        cases = {c["id"]: c for c in json.load(
            io.open(os.path.join(ROOT, cpath), encoding="utf-8"))["cases"]}
        rows = []
        with io.open(os.path.join(d, "raw.jsonl"), encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue                  # 残行同上
                case = cases.get(rec.get("id"))
                if case is not None:
                    rows.append(rescore(case, rec)["row"])
        return rows, summ

    ra, sa = load(a_tag)
    rb, sb = load(b_tag)
    ma = {r["id"]: r for r in ra}
    mb = {r["id"]: r for r in rb}
    sa, sb = sa["summ"], sb["summ"]      # summary.json 里指标嵌在 summ 下

    md = ["# 跨轮对比 %s → %s\n" % (a_tag, b_tag)]
    md.append("## 指标变化\n")
    md.append("| 指标 | %s | %s | Δ |" % (a_tag, b_tag))
    md.append("| --- | --- | --- | --- |")

    def pct(key):
        ok, n = sb[key]
        return fmt_pct(ok, n)
    for name, key in [("应答类 (AC-02)", "answer"), ("拒答类 (AC-04)", "refuse"),
                      ("跨产品 (AC-09)", "cross"), ("口语 (AC-10)", "oral"),
                      ("引用 (AC-03)", "cite")]:
        oka, na = sa[key]
        okb, nb = sb[key]
        d = (100.0 * okb / nb if nb else 0) - (100.0 * oka / na if na else 0)
        md.append("| %s | %d/%d | %d/%d | %+.0fpp |" % (name, oka, na, okb, nb, d))
    for name, key in [("串档率", "wrong_product"), ("误拒率", "over_refusal"), ("无据之答", "unsupported")]:
        oka, na = sa[key]
        okb, nb = sb[key]
        pa = 100.0 * oka / na if na else 0
        pb = 100.0 * okb / nb if nb else 0
        md.append("| %s（越低越好） | %s | %s | %+.0fpp |" % (name, fmt_pct(oka, na), fmt_pct(okb, nb), pb - pa))

    fixed = [i for i in mb if mb[i]["passed"] and not ma.get(i, {}).get("passed")]
    broke = [i for i in mb if not mb[i]["passed"] and ma.get(i, {}).get("passed")]
    md.append("\n## 逐条变化\n")
    md.append("**修好 %d 条**：%s\n" % (len(fixed), " ".join(fixed) or "无"))
    md.append("**改坏 %d 条**：%s\n" % (len(broke), " ".join(broke) or "无"))
    md.append("\n| id | 问题 | %s | %s |" % (a_tag, b_tag))
    md.append("| --- | --- | --- | --- |")
    for i in sorted(set(ma) | set(mb)):
        a = ma.get(i, {})
        b = mb.get(i, {})
        if a.get("passed") == b.get("passed"):
            continue
        md.append("| %s | %s | %s | %s |" % (
            i, b.get("question") or a.get("question"),
            "✅" if a.get("passed") else "❌ %s" % a.get("fail_class", ""),
            "✅" if b.get("passed") else "❌ %s" % b.get("fail_class", "")))
    out = "\n".join(md)
    p = os.path.join(RUNS, "compare_%s__%s.md" % (a_tag, b_tag))
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(out)
    print(out)
    print("\n对比报告: %s" % p)
    return 0


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--mode", choices=["sse", "blocking"], default="sse")
    ap.add_argument("--tag", help="本轮标识，用于目录名")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--only", help="只跑 id 前缀，逗号分隔，如 M-,C-,X-")
    ap.add_argument("--only-category", help="只跑某些 category，逗号分隔")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--resume", action="store_true",
                    help="复用已有 raw.jsonl 里跑过的用例，只补没跑的那些")
    ap.add_argument("--list-runs", action="store_true")
    args = ap.parse_args()

    if args.list_runs:
        if not os.path.isdir(RUNS):
            print("尚无运行记录")
            return 0
        for d in sorted(os.listdir(RUNS)):
            s = os.path.join(RUNS, d, "summary.json")
            if os.path.isfile(s):
                j = json.load(io.open(s, encoding="utf-8"))
                print("%-28s 通过 %2d/%-2d  串档 %d  误拒 %d  P50 %sms"
                      % (d, j["pass"], j["total"], j["summ"]["wrong_product"][0],
                         j["summ"]["over_refusal"][0], j["summ"]["p50"]))
        return 0

    if args.compare:
        return do_compare(*args.compare)

    if not args.tag:
        ap.error("跑单轮必须给 --tag")

    cases_path = args.cases if os.path.isabs(args.cases) else os.path.join(ROOT, args.cases)
    suite = json.load(io.open(cases_path, encoding="utf-8"))
    cases = suite["cases"]
    if args.only:
        pref = tuple(p.strip() for p in args.only.split(","))
        cases = [c for c in cases if c["id"].startswith(pref)]
    if args.only_category:
        cats = set(c.strip() for c in args.only_category.split(","))
        cases = [c for c in cases if c.get("category") in cats]
    if not cases:
        print("没有匹配到用例")
        return 2

    outdir = os.path.join(RUNS, args.tag)
    os.makedirs(outdir, exist_ok=True)
    raw_path = os.path.join(outdir, "raw.jsonl")

    # 参数快照：让每一轮的「被测配置」和结果绑在一起
    params = {}
    pp = os.path.join(ROOT, "eval", "params_snapshot.json")
    if os.path.isfile(pp):
        params = json.load(io.open(pp, encoding="utf-8"))

    print("评测 %d 条  mode=%s  workers=%d  tag=%s" % (len(cases), args.mode, args.workers, args.tag))
    print("取数: %s/api/chat/%s\n" % (args.base, "stream" if args.mode == "sse" else ""))

    t0 = time.time()
    results = []

    # 断点续跑：单轮全量在本机要十几分钟，超过单条命令的时限，
    # 所以允许分几次跑完一次评测，已跑过的用例原样保留、不重测。
    done_rows = {}
    mode_open = "a" if (args.resume and os.path.isfile(raw_path)) else "w"
    by_id = {c["id"]: c for c in cases}
    if mode_open == "a":
        bad = 0
        kept = {}                      # id -> rescore 结果，重复取最后一条
        order = []
        with io.open(raw_path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    # 进程被回收时最后一行可能只写了一半，丢掉它，不能让整轮续跑挂掉
                    bad += 1
                    continue
                case = by_id.get(rec.get("id"))
                if case is None:
                    continue
                if rec["id"] not in kept:
                    order.append(rec["id"])
                kept[rec["id"]] = rescore(case, rec)
        results.extend(kept[i] for i in order)
        done_rows.update({i: True for i in kept})
        dup = len(order) and sum(1 for i in order if order.count(i) > 1)
        print("续跑：已有 %d 条结果，跳过这些用例%s%s"
              % (len(kept), "（丢弃 %d 条残行）" % bad if bad else "",
                 "（合并重复 id）" if dup else ""))

    todo = [c for c in cases if c["id"] not in done_rows]
    if not todo:
        print("全部用例都跑过了，直接出报告")

    with io.open(raw_path, mode_open, encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(run_one, args.base, c, args.mode) for c in todo]
            for fut in futs:
                r = fut.result()          # 保序输出，便于人工对照
                fh.write(json.dumps(r["raw"], ensure_ascii=False) + "\n")
                fh.flush()                 # 逐条落盘：中途挂掉也留得住已跑的结果
                results.append(r)
                row = r["row"]
                print("  [%s] %-6s %-14s %4s  %5sms  %s"
                      % ("OK " if row["passed"] else "FAIL", row["id"], row.get("fail_class", ""),
                         ("%.2f" % row["coverage"]) if row.get("coverage") is not None else "-",
                         row.get("elapsed_ms"), row["question"][:30]))
    elapsed = time.time() - t0

    rows = [r["row"] for r in results]
    summ = summarize(rows)
    passed = sum(1 for r in rows if r["passed"])

    with io.open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"tag": args.tag, "cases": os.path.relpath(cases_path, ROOT),
                   "mode": args.mode, "base": args.base, "elapsed_s": round(elapsed, 1),
                   "pass": passed, "total": len(rows), "summ": summ,
                   "params": params}, f, ensure_ascii=False, indent=2)

    report = write_report(args.tag, rows, summ, params, cases_path, args.mode, args.base, elapsed)
    with io.open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)

    print("\n结果目录: %s" % outdir)
    print("通过 %d/%d | 应答 %d/%d | 拒答 %d/%d | 引用 %d/%d"
          % (passed, len(rows), summ["answer"][0], summ["answer"][1],
             summ["refuse"][0], summ["refuse"][1], summ["cite"][0], summ["cite"][1]))
    print("串档 %d/%d | 误拒 %d/%d | 无据之答 %d/%d"
          % (summ["wrong_product"][0], summ["wrong_product"][1],
             summ["over_refusal"][0], summ["over_refusal"][1],
             summ["unsupported"][0], summ["unsupported"][1]))
    print("P50 %dms  P95 %dms" % (summ["p50"], summ["p95"]))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
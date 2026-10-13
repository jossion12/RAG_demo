# -*- coding: utf-8 -*-
"""漏斗归因：把端到端的「过/不过」翻译成「卡在哪一环、该拧哪个旋钮」。

读两份数据，拼出一张能指导调参的表
--------------------------------------
    trace.jsonl  （eval/trace_stages.py 产出）检索侧三段漏斗 + 语料层判定
    raw.jsonl    （eval/run_iteration.py 产出）端到端的答案与引用

`run_iteration.py` 的归因（citation_miss / over_refusal / …）是**结果层**的：
它能告诉你「引用没兜住期望条款」，但五根手指都指着同一个方向。每种卡点
要拧的旋钮完全不同：

    chunk_missing    库里压根没有这段      →  调阈值/换 rerank 全都无效，回去改入库与分段
    doc_missing      期望那份文档没入库    →  补录该条款（会改评测口径，需单独基准）
    cross_doc        同名章节只在别的文档里 →  拆库或加产品路由，调参只会更糟
    recall_miss      库里有，向量没捞上来   →  调权重 / top_k / embedding
    rerank_dropped   捞上来了，rerank 压掉  →  换 rerank 模型或改取舍策略
    threshold_cut    rerank 过了，阈值砍掉  →  降 score_threshold
    llm_miss         全过了，LLM 没写出来  →  改 prompt / 换模型

用法
----
    python eval/attribute_funnel.py --tag t001
    python eval/attribute_funnel.py --tag t001 --raw r005_notoc
    python eval/attribute_funnel.py --tag t001 --raw r005_notoc --params-sweep

--params-sweep 会额外给出**建议阈值**：把所有 threshold_cut 用例在 B 段
拿到的分数摊开，取能覆盖八成的分位数。这一步是把归因变成动作的关键——
只说「被阈值砍了」没用，得说「降到多少能捞回来」。
"""
import argparse
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))
import run_iteration as R                                      # noqa: E402

RUNS = os.path.join(ROOT, "eval", "runs")
CASES = os.path.join(ROOT, "eval", "eval_cases_v2.json")

A, B, C = "A_retrieval_only", "B_reranked_full", "C_live_params"

# 瓶颈分类 -> (中文名, 该拧哪个旋钮, 危险度)
# 危险度沿用「客户会不会拿到错误结论」的口径，与 run_iteration.SEVERITY 对齐：
# 调参没危险度排序时，最容易先花时间在低危项上。
BOTTLENECK = {
    "chunk_missing":     ("切割/入库漏了",       "重切分段或补录；调阈值无效", 2),
    "doc_missing":       ("期望文档未入库",     "补录该条款；会改评测口径", 2),
    "cross_doc":         ("同名章节串到别的文档", "拆库 / 加产品路由", 3),
    "recall_miss":       ("向量召回没捞上来",    "调权重 / top_k / embedding", 1),
    "rerank_dropped":    ("rerank 把它压掉了",  "换 rerank 模型或改取舍策略", 2),
    "threshold_cut":     ("阈值把它砍掉了",      "降 score_threshold", 1),
    "llm_miss":          ("LLM 没写出来",       "改 prompt / 换模型", 2),
    "snippet_truncated": ("snippet 截断导致误判", "改评测判据用完整原文，不是检索缺陷", 0),
    "trace_conflict":    ("trace 与端到端矛盾", "同轮重采后再判，别急着改代码", 0),
    "noise_only":        ("召回的都是噪声",     "调阈值反而更糟，需改提问路由", 2),
    "ok":                ("全程贯通",           "无需动作", 0),
    "infra":             ("环境问题",           "先修环境再谈归因", 0),
}


def norm(s):
    return re.sub(r"\s+", "", s or "")


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


def downdrill(rec):
    """从最下游往上游找**第一个**丢失期望章节的环节。

    顺序必须是 C -> B -> A：先问「最终没进去」，再问「是哪一环弄丢的」。
    反过来查会把「阈值砍掉」误报成「rerank 压掉」——它们症状一样（排名没了），
    区别只在断点位置。
    """
    st = rec["stages"]
    cinfo = rec["corpus"]
    cv = cinfo["verdict"]

    # 环境：rerank 没就绪时 B/C 恒空，这会让后面每一级都误判成「被砍」。
    if rec.get("infra") == "rerank_unready":
        return "infra", "A 段有结果但 B 段空 —— TEI rerank 未就绪，本轮检索侧结论不可用"

    # 语料层先判：库里没有，后面全是白查。
    if cv == "absent":
        return "chunk_missing", ("期望章节标题在全库都搜不到"
                                 "（title_hits=%d）" % cinfo["title_hits"])
    if cv == "doc_absent":
        return "doc_missing", ("标题只在别的文档里以别的编号出现，期望文档没有这段"
                               "（title_hits=%d, exact_hits=%d）"
                               % (cinfo["title_hits"], cinfo["exact_hits"]))
    if cv == "wrong_doc":
        return "cross_doc", "编号+标题对得上，但命中片段全来自别的产品文档"

    cs, bs, as_ = st.get(C, {}), st.get(B, {}), st.get(A, {})

    # 拒答类用例的漏斗是**反向**的：C 段本来就不该有期望章节。
    if rec.get("expect_refuse"):
        if cs.get("n", 0) == 0:
            return "ok", "拒答类：C 段为空，符合期望"
        if cs.get("cite_hit") == "exact":
            return "threshold_cut", "拒答类却把期望章节喂给了 LLM —— 阈值过低会诱发凭噪声作答"
        return "noise_only", "拒答类 C 段有 %d 条内容但不含期望章节" % cs.get("n", 0)

    # 无锚点用例（口语化等）没有期望章节可比，只能看 LLM 侧，由调用方补。
    if cv == "no_anchor":
        return None, "该用例无 expected_clause，检索侧无锚点可比"

    if cs.get("best_rank") is not None:
        return "ok", "期望章节在 C 段第 %d 名，进了 LLM 上下文" % cs["best_rank"]
    if bs.get("best_rank") is not None:
        return "threshold_cut", ("期望章节在 B 段第 %d 名（%.4f），"
                                 "被阈值 %.2f 砍掉"
                                 % (bs["best_rank"], bs["best_score"],
                                    _live_threshold()))
    if as_.get("best_rank") is not None:
        drop = as_["best_score"]
        return "rerank_dropped", ("期望章节在 A 段第 %d 名（%.4f），"
                                  "rerank 后掉出候选"
                                  % (as_["best_rank"], drop))
    if cs.get("cite_hit") == "title_only" or bs.get("cite_hit") == "title_only":
        return "cross_doc", "只捞到同名章节，编号对不上（另一份文档的同名章节）"
    return "recall_miss", ("期望章节不在 A 段前 %d 条候选里（库里有，"
                           "但向量+关键词没把它捞上来）" % as_.get("n", 0))


_LIVE_TH = [0.1]


def _live_threshold():
    return _LIVE_TH[0]


# --------------------------------------------------------------------------
# 自检：把 downdrill 的每条分支钉死
# --------------------------------------------------------------------------
def _mk_rec(cv="present", a_rank=None, b_rank=None, c_rank=None,
            a_n=32, b_n=32, c_n=6, refuse=False, infra=None):
    """构造一条最小 trace 记录，只喂 downdrill 真正读到的字段。

    注意构造的是**结构**而不是真实检索结果——自检要验的是
    「给定这样的漏斗形状，该归到哪一类」，不依赖网络与 Dify。
    """
    def st(n, rank):
        return {"n": n, "cite_hit": "exact" if rank else False,
                "best_rank": rank, "best_score": 0.5 if rank else None}

    return {"id": "T-00", "question": "自检用例", "expect_refuse": refuse,
            "infra": infra,
            "corpus": {"verdict": cv, "title_hits": 3,
                       "exact_hits": 1 if cv in ("present", "wrong_doc") else 0},
            "stages": {A: st(a_n, a_rank), B: st(b_n, b_rank), C: st(c_n, c_rank)}}


# (期望归因, 构造参数)
_SELFTEST = [
    # 语料层：库里压根没有，后面的层级一律不查
    ("chunk_missing", {"cv": "absent", "a_rank": 1, "c_rank": 1}),
    ("doc_missing",   {"cv": "doc_absent", "a_rank": 1, "c_rank": 1}),
    ("cross_doc",     {"cv": "wrong_doc", "a_rank": 1, "c_rank": 1}),
    # 环境：rerank 没就绪时 B/C 恒空，必须先于所有内容判断
    ("infra",         {"cv": "present", "infra": "rerank_unready", "a_rank": 1}),
    # 检索层：从下游往上游找第一个丢掉的环节
    ("ok",             {"cv": "present", "c_rank": 1}),
    ("threshold_cut",  {"cv": "present", "b_rank": 3}),
    ("rerank_dropped", {"cv": "present", "a_rank": 5}),
    ("recall_miss",    {"cv": "present"}),
    # 拒答类：漏斗是反向的，C 段本来就不该有期望章节
    ("ok",             {"cv": "present", "c_n": 0, "refuse": True}),
    ("noise_only",     {"cv": "present", "c_n": 5, "refuse": True}),
    ("threshold_cut",  {"cv": "present", "c_rank": 2, "refuse": True}),
    # 无锚点：检索侧无从比对
    (None,             {"cv": "no_anchor", "a_rank": 1, "c_rank": 1}),
]


def selftest():
    bad = []
    for want, kw in _SELFTEST:
        got, why = downdrill(_mk_rec(**kw))
        label = kw.get("cv")
        if got != want:
            bad.append("corpus=%-11s 期望 %-15s 实际 %-15s" % (label, want, got))
        if not why:
            bad.append("corpus=%-11s 归因 %s 缺 why 文案" % (label, want))
    print("downdrill 自检 %d 条" % len(_SELFTEST))
    for b in bad:
        print("  [x] " + b)
    if not bad:
        print("  [OK] 全部通过")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="t001")
    ap.add_argument("--raw", default="", help="端到端那一轮的 run tag，如 r005_notoc")
    ap.add_argument("--threshold", type=float, default=None,
                    help="线上 score_threshold，仅用于报告里的措辞，默认读 params_snapshot")
    ap.add_argument("--params-sweep", action="store_true", help="额外输出建议阈值")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑归因分支自检（不触网、不读 trace.jsonl）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    trace_path = os.path.join(RUNS, args.tag, "trace.jsonl")
    traces = load_jsonl(trace_path)
    if not traces:
        print("没有 trace：%s\n先跑：python eval/trace_stages.py --tag %s"
              % (trace_path, args.tag))
        return 2

    if args.threshold is not None:
        _LIVE_TH[0] = args.threshold
    else:
        try:
            with io.open(os.path.join(ROOT, "eval", "params_snapshot.json"), encoding="utf-8") as f:
                _LIVE_TH[0] = json.load(f)["dataset_retrieval_model"]["score_threshold"]
        except Exception:                                       # noqa: BLE001
            pass

    with io.open(CASES, encoding="utf-8") as f:
        cases = {c["id"]: c for c in json.load(f)["cases"]}

    raw_rows = {}
    if args.raw:
        raw_rows = {r["id"]: r for r in load_jsonl(os.path.join(RUNS, args.raw, "raw.jsonl"))}
        if not raw_rows:
            print("[!] 找不到 raw.jsonl（--raw %s），将只出检索侧归因" % args.raw)

    out_dir = os.path.join(RUNS, args.tag)
    detail = []
    for rec in traces:
        case = cases.get(rec["id"], {})
        bn, why = downdrill(rec)

        end2end, llm_why = None, ""
        raw_chit = None
        if rec["id"] in raw_rows:
            res = raw_rows[rec["id"]]["res"]
            passed, cls, info = R.classify(case, res)
            end2end = "pass" if passed else cls
            llm_why = info.get("why", "")
            raw_chit = info.get("cite_hit")
            # 检索侧说「一路通」，端到端却没过 —— 先问一句：端到端引用里
            # **真的**兜住期望章节了吗？
            #   兜住了（cite_hit=exact）  -> 内容确实到了 LLM 手里，是生成侧问题
            #   没兜住                   -> 不能赖 LLM。trace 与端到端对不上，
            #     多半是两侧不是**同一次调用**（raw 是旧轮、trace 是刚采的），
            #     或两者参数已变。这种矛盾必须单列，不能顺手归到生成侧，
            #     否则会拿着「改 prompt」的结论去修一个根本不是 prompt 的问题。
            if bn == "ok" and not passed and cls not in ("error", "blocked"):
                trace_chit = rec["stages"].get(C, {}).get("cite_hit")
                if info.get("cite_hit") == "exact":
                    bn = "llm_miss"
                    why = ("检索侧全程贯通（期望章节第 %s 名进了上下文），"
                           "端到端引用也兜住了它，但答案仍判 %s：%s"
                           % (rec["stages"].get(C, {}).get("best_rank"), cls, llm_why))
                elif trace_chit == "exact":
                    # 两侧不一致，且**trace 侧用完整原文判是命中的**：
                    # 差异来自治理层把 snippet 截到 300 字，章节标题落在截断之外。
                    # 这是评测口径问题，不是检索问题——单列出来，别混进检索缺陷。
                    bn = "snippet_truncated"
                    why = ("检索侧期望章节在 C 段第 %s 名（完整原文判定命中），"
                           "端到端却判 %s：差异来自 snippet 被截到 300 字，"
                           "章节标题落在截断之外。属评测口径问题，不是检索缺陷"
                           % (rec["stages"].get(C, {}).get("best_rank"), cls))
                else:
                    bn = "trace_conflict"
                    why = ("trace 说期望章节在 C 段第 %s 名，端到端 %s 却判引用没兜住"
                           "（cite_hit=%s）——两侧不是同一次调用或参数已变，"
                           "同轮重采后才能定论"
                           % (rec["stages"].get(C, {}).get("best_rank"), cls,
                              info.get("cite_hit")))

        detail.append({
            "id": rec["id"], "product": rec.get("product"),
            "category": rec.get("category"), "question": rec["question"],
            "expected_clause": rec.get("expected_clause"),
            "corpus_verdict": rec["corpus"]["verdict"],
            "bottleneck": bn or "no_anchor",
            "why": why,
            "end_to_end": end2end, "end_to_end_why": llm_why,
            # 同一件事的两种判读：raw 用治理层截断后的 snippet 判，
            # trace 用检索层返回的完整原文判。两者不一致即口径偏差（见报告）。
            "cite_hit_raw": raw_chit,
            "cite_hit_trace": rec["stages"].get(C, {}).get("cite_hit"),
            "funnel": {k: {"n": rec["stages"][k]["n"],
                           "best_rank": rec["stages"][k]["best_rank"],
                           "best_score": rec["stages"][k]["best_score"]}
                       for k in (A, B, C) if k in rec["stages"]},
        })

    with io.open(os.path.join(out_dir, "attribution.jsonl"), "w", encoding="utf-8") as f:
        for d in detail:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    write_report(out_dir, args.tag, detail, args.raw, args.params_sweep, recs=traces)
    return 0


def write_report(out_dir, tag, detail, raw_tag, sweep, recs=None):
    md = []
    md.append("# 漏斗归因报告 · %s\n" % tag)
    md.append("- 生成时间：%s" % __import__("time").strftime("%Y-%m-%d %H:%M:%S"))
    md.append("- 端到端数据：%s" % (raw_tag if raw_tag else "未提供（仅检索侧归因）"))
    md.append("- 阈值取自线上快照：%.2f\n" % _live_threshold())

    # 跨轮混用会让结论失真，必须在报告最显眼处说明。
    # trace 采于某一时刻、raw 跑于另一时刻——两者之间只要动过参数，
    # 「trace 说期望章节进了上下文 / raw 说引用里没有」就会同时成立，
    # 这不是矛盾，是两次不同的调用。
    conflicts = [d for d in detail if d["bottleneck"] == "trace_conflict"]
    if raw_tag and conflicts:
        md.append("\n> ⚠️ **本报告混用了两轮数据**：检索侧 trace 采于 "
                 "`eval/runs/%s/trace.jsonl`，端到端取自 `%s`。"
                 "两者不是同一次调用，期间参数或索引可能已变——"
                 "有 **%d 条** 因此无法定论，被标为 `trace_conflict`。"
                 "要得到干净结论，必须同轮重采：先跑 `run_iteration.py --raw <tag>`，"
                 "紧接着跑 `trace_stages.py --tag <tag>`，再回到本脚本。\n"
                 % (tag, raw_tag, len(conflicts)))

    # ---- 瓶颈分布 ----
    md.append("\n## 瓶颈分布（这才是该拧哪个旋钮）\n")
    md.append("| 瓶颈 | 条数 | 用例 | 旋钮 | 危险度 |")
    md.append("| --- | --- | --- | --- | --- |")
    order = sorted([k for k in BOTTLENECK],
                   key=lambda k: (-BOTTLENECK[k][2], k))
    for k in order:
        rows = [d for d in detail if d["bottleneck"] == k]
        if not rows:
            continue
        name, knob, sev = BOTTLENECK[k]
        ids = ", ".join(d["id"] for d in rows[:12]) + ("…" if len(rows) > 12 else "")
        md.append("| `%s` %s | %d | %s | %s | %s |"
                  % (k, name, len(rows), ids, knob, "🔴" * sev))

    # ---- 端到端 × 瓶颈 交叉 ----
    with_e2e = [d for d in detail if d["end_to_end"]]
    if with_e2e:
        md.append("\n## 端到端失败 → 实际卡在哪一环\n")
        md.append("这张交叉表是本报告的核心：`run_iteration` 看到的是同一个"
                  "`citation_miss`，拆开看可能分别卡在切割、召回、阈值、生成，"
                  "解法完全不同。\n")
        classes = sorted({d["end_to_end"] for d in with_e2e if d["end_to_end"] != "pass"})
        all_bn = [k for k in BOTTLENECK if any(d["bottleneck"] == k for d in with_e2e)]
        md.append("| 端到端归因 | 条数 | " + " | ".join("`%s`" % b for b in all_bn) + " |")
        md.append("| --- | --- | " + " | ".join("---" for _ in all_bn) + " |")
        for cls in classes:
            rows = [d for d in with_e2e if d["end_to_end"] == cls]
            cells = []
            for b in all_bn:
                n = sum(1 for d in rows if d["bottleneck"] == b)
                cells.append(str(n) if n else "")
            md.append("| `%s` | %d | %s |" % (cls, len(rows), " | ".join(cells)))

    # ---- 判据偏差：截断 snippet vs 完整原文 ----
    judged = [d for d in detail if d.get("cite_hit_raw") is not None]
    diff = [d for d in judged if d["cite_hit_raw"] != d["cite_hit_trace"]]
    if judged and diff:
        md.append("\n## ⚠️ 判据偏差：用展示用 snippet 判章节命中会误判\n")
        md.append("治理层把引用片段截到 **300 字**（`CitationAssembler.SNIPPET_MAX`），"
                  "这是前端展示该有的样子。但条款片段中位 624 字、最长 1019 字，"
                  "**章节标题常落在 300 字之外**。`run_iteration.py` 的 `cite_hit` "
                  "读的正是这个截断后的 `snippet`，于是把「明明召回到了」判成 "
                  "`citation_miss` / `clause_mismatch`。\n")
        md.append("这不是检索问题，也不是治理层的 bug（展示层就该截断），"
                  "**是评测拿展示字段当判据用**。下表是同一批引用的两种判读：\n")
        md.append("| 用例 | 期望章节 | raw 判（截断 snippet） | trace 判（完整原文） | 端到端归因 |")
        md.append("| --- | --- | --- | --- | --- |")
        for d in diff:
            md.append("| %s | `%s` | `%s` | `%s` | `%s` |"
                      % (d["id"], d["expected_clause"], d["cite_hit_raw"],
                         d["cite_hit_trace"], d["end_to_end"]))
        md.append("\n**%d/%d 条**的端到端归因受此影响。"
                  "处理方式：要么让治理层额外返回一个未截断的字段供评测用，"
                  "要么评测侧直接读 trace 的完整原文（本文档的漏斗列即由此得出）。"
                  "在此之前，这几类的数量**不能直接当成检索缺陷**。\n"
                  % (len(diff), len(judged)))

    # ---- 逐条明细 ----
    md.append("\n## 逐条明细\n")
    for d in detail:
        st = d["funnel"]
        md.append("\n### %s · %s\n" % (d["id"], d["bottleneck"]))
        md.append("- 问题：%s" % d["question"])
        if d["expected_clause"]:
            md.append("- 期望章节：`%s`（语料层 `%s`）"
                      % (d["expected_clause"], d["corpus_verdict"]))
        md.append("- 漏斗：A %s 条(第%s名) → B %s 条(第%s名) → C %s 条(第%s名)"
                  % (st.get(A, {}).get("n"), st.get(A, {}).get("best_rank"),
                     st.get(B, {}).get("n"), st.get(B, {}).get("best_rank"),
                     st.get(C, {}).get("n"), st.get(C, {}).get("best_rank")))
        md.append("- 归因：%s" % d["why"])
        if d["end_to_end"]:
            md.append("- 端到端：`%s` %s" % (d["end_to_end"], d["end_to_end_why"]))

    if sweep:
        md.append(sweep_section(recs or []))

    path = os.path.join(out_dir, "funnel_report.md")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print("归因 %d 条" % len(detail))
    counts = {}
    for d in detail:
        counts[d["bottleneck"]] = counts.get(d["bottleneck"], 0) + 1
    for k in sorted(counts, key=lambda x: -counts[x]):
        print("  %-18s %d" % (k, counts[k]))
    print("报告 -> %s" % path)


def sweep_section(recs):
    """给出一个**可执行**的阈值建议。

    只统计「确实是被阈值砍掉」的用例：在 B 段（阈值关闭）排得上、
    到 C 段（线上阈值）就掉了，且语料层判定内容确实在库里。
    它们的 B 段分数就是要捞回它们时阈值必须**低于**的那个值——
    所以建议值取的是这批分数的**低分位**，且必然小于当前阈值。

    ⚠️ 这里曾经写反：拿全体用例分数的高分位当建议值，输出了
    「降阈值到 0.53」——而线上阈值只有 0.10，那是**提高**不是降低。
    建议值大于当前阈值就说明取样或逻辑错了，宁可不出建议也不出错的。
    """
    cut = []
    for rec in recs:
        b = rec["stages"].get(B, {})
        c = rec["stages"].get(C, {})
        if (b.get("best_rank") is not None and c.get("best_rank") is None
                and rec["corpus"]["verdict"] == "present"
                and not rec.get("expect_refuse")):
            cut.append(b["best_score"])

    md = ["\n## 参数建议：阈值该降到多少\n"]
    if not cut:
        md.append("本轮没有被阈值砍掉的用例——阈值不是当前瓶颈，调它只会引入噪声。")
        return "\n".join(md)

    cut.sort()
    n = len(cut)
    lo = cut[0]                          # 全捞回：阈值需低于此值
    p20 = cut[max(0, int(n * 0.2) - 1)]  # 捞回约八成
    cur = _live_threshold()

    md.append("下列用例的期望章节**在 B 段（阈值关闭）排得上、到 C 段（线上阈值 %.2f）就掉了**，"
              "且语料层确认内容确实在库里——它们是被阈值挡住的，不是没召回。\n" % cur)
    md.append("| 条数 | 最低 | P20 | P50 | 最高 |")
    md.append("| --- | --- | --- | --- | --- |")
    md.append("| %d | %.4f | %.4f | %.4f | %.4f |"
              % (n, lo, p20, cut[n // 2], cut[-1]))

    if p20 >= cur:
        # 取样或逻辑自相矛盾：能被阈值砍掉的用例，分数不该高于阈值。
        # 这时给建议等于给错的方向，直接把矛盾暴露出来而不是硬凑一个数。
        md.append("\n> ⚠️ 取样异常：被判为「被阈值砍掉」的用例里，P20 分数 %.4f ≥ 当前阈值 %.2f。"
                  "这些用例不可能是被阈值挡住的，请检查 trace 是否与 raw 来自同一次采集。"
                  "本轮不给阈值建议。" % (p20, cur))
        return "\n".join(md)

    target = max(0.0, p20 - 0.01)
    md.append("\n把阈值从 **%.2f** 降到 **%.2f** 可捞回其中约八成"
              "（全部捞回需低于 **%.4f**）。\n"
              % (cur, target, lo))

    if target <= 0.02:
        # 分数这么低，说明这些片段与问题本来就不相关——
        # 降阈值「捞」回来的不是正确内容，是噪声里碰巧同主题的东西。
        # 这时给建议不如直接说清楚：这条路走不通。
        md.append("> **但这个建议不要照做。** 建议值已逼近 0，等于把整个候选池"
                  "（每条查询 32 条）全部交给 LLM。被挡住的这些片段分数低到 "
                  "%.4f，说明它们与问题**本来就不相关**——放进来不是「捞回正确答案」，"
                  "而是让模型从一堆无关文本里找一个勉强沾边的说法。\n\n"
                  "**结论：这 %d 条不是阈值的参数问题。** 它们的正解分别是：\n"
                  "  - 语料/表述层面：这类问法的关键表述在库里没有对应的强相关片段，"
                  "需要检查是不是问法与条款措辞差太远（看 `trace.jsonl` 里 "
                  "`A_retrieval_only` 段的排名与分数）；\n"
                  "  - 架构层面：与 `cross_doc` 一样，需要先做产品路由再谈阈值。\n\n"
                  % (lo, len(cut)))
    else:
        md.append("⚠️ 降阈值会同时放进更多噪声片段——分数落在 [%.2f, %.2f) 的片段本来被挡着，"
                  "降完它们会一起进 LLM。动之前先跑 "
                  "`python scripts/sweep_retrieval.py --stage threshold` 看噪声曲线，"
                  "**不要只看命中数**：串档（`cross_doc`）恰恰是被放出来的噪声喂出来的，"
                  "阈值降得越多，串档越多。\n" % (target, cur))
    return "\n".join(md)


if __name__ == "__main__":
    sys.exit(main())
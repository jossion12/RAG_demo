# -*- coding: utf-8 -*-
"""链路 trace 采集：把「一次问答经过的每一环」逐环落盘。

解决什么问题
------------
`run_iteration.py` 的归因是**端到端一层**。`citation_miss` / `over_refusal`
只回答了「引用没兜住期望条款」，答不出**卡在哪一环**——而每种卡点要用的
旋钮完全不同：

    库里就没有这段        -> 调阈值 / 换 rerank 全都无效，得回去改入库与分段
    库里有，向量没捞上来   -> 调权重 / top_k / embedding
    捞上来了，rerank 压掉  -> 换 rerank 模型，或改 rerank 前后的取舍策略
    rerank 过了，阈值砍掉  -> 降 score_threshold
    都过了，LLM 没写出来  -> 改 prompt / 换模型

把这五种混在一个 `passed=False` 里，调参就只能靠猜，且每轮 15 分钟的
全量评测会把「猜错方向」的代价放大到整轮报废。

采集方式：**对照探针**，同一个 query 只改一个旋钮，各跑一次
    A  关 rerank + 阈值 0 + top_k 32  ->  混合检索的原始候选池
    B  开 rerank + 阈值 0 + top_k 32  ->  rerank 之后的全部分数（未被阈值砍）
    C  线上原样                     ->  真正进入 LLM 的那批
再叠一层**语料层**判定（corpus.json，见 scripts/dump_corpus.py）：
    期望章节此刻在不在库里？在哪份文档？编号对不对？

这样每个用例都得到一条完整的、可复查的漏斗轨迹，而不是一个布尔值。
只检索不调 LLM，52 条约 5~8 分钟，比全量评测（15 分钟）便宜一个量级。

产物
----
`eval/runs/<tag>/trace.jsonl`，每行一个用例：

    {
      "id": "M-01",
      "question": "...",
      "expected_clause": "2.3 等待期",
      "corpus":  {"verdict": "present", "exact_hits": 3, "where": [...]},
      "stages":  {"A_retrieval_only": {"n": 32, "best_rank": 3, ...},
                  "B_reranked_full": {"n": 32, "best_rank": 1, ...},
                  "C_live_params":   {"n": 6,  "best_rank": 1, ...}},
      "pool":    {"A_retrieval_only": [{doc,pos,score,head}, ...], ...}
    }

用法
----
    python eval/trace_stages.py --tag t001
    python eval/trace_stages.py --tag t001 --resume      # 中断续跑
    python eval/trace_stages.py --tag t001 --only M-01,M-02
    python scripts/verify_trace_funnel.py                # 先验漏斗结构成立

前置：Dify 已启动，且 TEI rerank 已 ready。
TEI 冷启动要 60~90 秒加载模型；没 ready 时 B/C 段会**静默返回 0 条**——
脚本会把这种情况标成 `infra_rerank_unready` 而不是当成「没召回」。
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))
import run_iteration as R                                      # noqa: E402

DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".dify_dataset_key")
CASES = os.path.join(ROOT, "eval", "eval_cases_v2.json")
SNAPSHOT = os.path.join(ROOT, "eval", "params_snapshot.json")
CORPUS = os.path.join(ROOT, "eval", "runs", "corpus.json")
RUNS = os.path.join(ROOT, "eval", "runs")

CANDIDATE_CAP = 32      # 候选池上限。>=33 会静默返回空集（见 README）
POOL_KEEP = 32          # 每段留多少条明细；候选池本来就 <=32，等于全留

STAGES_NOTE = """三个阶段的旋钮组合见 stages_for()：刻意只差一个变量。
    A 关 rerank + 阈值 0  ->  混合检索的原始候选池
    B 开 rerank + 阈值 0  ->  rerank 之后的全部分数（未被阈值砍）
    C 线上原样            ->  真正进入 LLM 的那批
    top_k 硬上限 32：>=33 会静默返回空集（见 README）。
"""


def stages_for(live_override):
    """三段探针。刻意只差一个变量：
        A vs B 只差 rerank  -> 差异归 rerank
        B vs C 只差阈值    -> 差异归阈值
    C 段的参数默认取线上快照，也可以用 --live-* 覆盖，用来跑**对照实验**
    （比如验证「降阈值会放进噪声」这个预测）——这一步是闭环的最后一环：
    没有对照，归因给出的建议就只是说法。
    """
    return [
        ("A_retrieval_only", {"reranking_enable": False, "score_threshold": 0.0,
                               "top_k": CANDIDATE_CAP}),
        ("B_reranked_full", {"reranking_enable": True, "score_threshold": 0.0,
                             "top_k": CANDIDATE_CAP}),
        ("C_live_params", live_override),      # None = 线上快照原样
    ]


def norm(s):
    return re.sub(r"\s+", "", s or "")


# --------------------------------------------------------------------------
# 语料层：期望章节此刻在不在库里
# --------------------------------------------------------------------------
def load_corpus():
    if not os.path.isfile(CORPUS):
        return None
    with io.open(CORPUS, encoding="utf-8") as f:
        return json.load(f)


def doc_product(name):
    """文档名 -> 产品代号；未登记的文档返回 None（别默认放行）。"""
    for doc, prod in R.DOC_PRODUCT.items():
        if doc in (name or ""):
            return prod
    return None


def corpus_verdict(case, corpus):
    """判定期望章节在**语料层**的状态。

    三档而不是两档，因为「标题能搜到」和「这段真的是它」不是一回事：
        present    期望文档里存在同编号+同标题的片段 -> 后面只需看检索侧
        wrong_doc  编号+标题对得上，但命中片段**全来自别的产品**
                   -> 串档，正解是拆库/加产品路由，调阈值只会更糟
        doc_absent 标题只在别的文档里以别的编号出现 -> 期望文档没入库
                   或评测集指向了未入库的条款（团体的 G-01~G-05 就是这种）
        absent     标题在全库都搜不到 -> 真·切割/入库问题

    这一层是整套归因的地基：它把「内容压根不在库里」和「在库里没捞上来」
    分开，而这两件事的解法完全不相干。
    """
    clause = case.get("expected_clause")
    if not clause:
        return {"verdict": "no_anchor", "title_hits": 0, "exact_hits": 0, "where": []}
    if corpus is None:
        return {"verdict": "corpus_missing", "title_hits": 0, "exact_hits": 0, "where": []}

    m = re.match(r"^\s*([\d.]+)\s+(.*)$", clause.strip())
    num, title = (m.group(1), norm(m.group(2))) if m else (None, norm(clause))

    allow = R.CASE_PRODUCTS.get(case.get("product"), set())
    where, title_hits, exact_hits = [], 0, 0
    for d in corpus["documents"]:
        for s in d["segments"]:
            blob = norm(s.get("content") or "")
            if not title or title not in blob:
                continue
            title_hits += 1
            if num and num in blob:
                exact_hits += 1
                where.append({"doc": d["name"], "pos": s.get("position"),
                              "product": doc_product(d["name"])})
    if not exact_hits:
        verdict = "doc_absent" if title_hits else "absent"
    elif allow and not any(w["product"] in allow for w in where):
        # 编号+标题对得上，但**命中片段全部来自别的产品**。
        # 例：G-04 期望团体条款的「2.5 免赔额」，库里同编号同标题的只有个人医疗那份。
        # 这种情况对用户就是串档——「内容在库里」这句话会误导调参方向
        # （会以为该调阈值，其实该拆库或加产品路由）。所以单独成档。
        verdict = "wrong_doc"
    else:
        verdict = "present"

    # 命中的片段是否都来自非期望产品——用于把 doc_absent 说得更具体
    wrong_doc = bool(where) and not any(w["product"] in allow for w in where)
    return {"verdict": verdict, "title_hits": title_hits, "exact_hits": exact_hits,
            "wrong_product_only": wrong_doc, "where": where[:8]}


# --------------------------------------------------------------------------
# 检索层：三段对照探针
# --------------------------------------------------------------------------
def load_base():
    with io.open(SNAPSHOT, encoding="utf-8") as f:
        return json.load(f)["dataset_retrieval_model"]


def retrieve(tok, query, rm, timeout=180):
    body = json.dumps({"query": query, "retrieval_model": rm}).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost/v1/datasets/%s/retrieve" % DATASET_ID,
        data=body,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    out = []
    for x in d.get("records") or []:
        seg = x.get("segment") or {}
        out.append({
            "score": float(x.get("score") or 0.0),
            "doc": (seg.get("document") or {}).get("name", ""),
            "pos": seg.get("position"),
            # 判定必须用**本次探针真实返回的完整原文**，不能截断，也不能去
            # 语料快照里取。章节标题常落在片段中部（中位 624 字），截断到 80 字
            # 会让「明明召回到了」的用例被判成没召回到——判据一旦用副本，
            # 整张漏斗表就会静默失真。完整原文在 probe_case 里按 (doc,pos)
            # 去重后落盘，pool 只存引用。
            "content": seg.get("content") or "",
        })
    return out


def stage_stats(case, rows):
    """这一段里，期望章节排第几、分数多少。

    判分复用 run_iteration.cite_hit（同一套 exact/title_only/False 三档），
    保证 trace 的「召回到了」和评测报告的「引用命中」是同一个口径——
    两套口径会在跨表对比时打架。
    """
    cites = [{"label": r["doc"], "snippet": r["content"]} for r in rows]
    clause = case.get("expected_clause")
    hit = R.cite_hit(cites, clause) if clause else None
    rank, score = None, None
    for i, r in enumerate(rows, 1):
        one = [{"label": r["doc"], "snippet": r["content"]}]
        if clause and R.cite_hit(one, clause) == "exact":
            rank, score = i, r["score"]
            break
    return {"n": len(rows), "cite_hit": hit,
            "best_rank": rank, "best_score": score,
            "top_score": rows[0]["score"] if rows else None,
            "products": sorted({doc_product(r["doc"]) for r in rows
                                if doc_product(r["doc"])})}


def probe_case(tok, case, base, corpus, live_override=None):
    rec = {"id": case["id"], "question": case["question"],
           "product": case.get("product"), "category": case.get("category", ""),
           "expect_refuse": bool(case.get("expect_refuse")),
           "expected_clause": case.get("expected_clause"),
           "corpus": corpus_verdict(case, corpus),
           "stages": {}, "pool": {}, "segs": {}, "error": None}
    for name, override in stages_for(live_override):
        rm = json.loads(json.dumps(base))
        if override:
            rm.update(override)
        try:
            rows = retrieve(tok, case["question"], rm)
        except urllib.error.HTTPError as e:
            rec["error"] = "%s HTTP %s" % (name, e.code)
            return rec
        except Exception as e:                                    # noqa: BLE001
            rec["error"] = "%s %s: %s" % (name, type(e).__name__, str(e)[:120])
            return rec
        rec["stages"][name] = stage_stats(case, rows)
        # pool 只存 (doc,pos,score) + 片段引用，完整原文在 rec["segs"] 里
        # 按 (doc,pos) 去重存一份。同一片段通常同时出现在 A/B/C 三段，
        # 不去重会让 trace 文件白白大三倍，且看不出「三段看到的是同一批」。
        pool = []
        for r in rows[:POOL_KEEP]:
            key = "%s#%s" % (r["doc"], r["pos"])
            rec["segs"][key] = r["content"]
            pool.append({"doc": r["doc"], "pos": r["pos"],
                         "score": r["score"], "seg": key})
        rec["pool"][name] = pool
    # A 段正常而 B 段空 = rerank 侧没就绪（TEI 冷启动/挂掉），不是内容问题。
    # 这个坑不标出来，会把环境故障整批误判成「召回没命中」。
    if rec["stages"]["A_retrieval_only"]["n"] > 0 and rec["stages"]["B_reranked_full"]["n"] == 0:
        rec["infra"] = "rerank_unready"
    return rec


def selftest():
    """语料层四档判定的自检。

    这一层是整套 trace 的地基——它决定后面所有层级还有没有得查，
    所以四种判定（present / wrong_doc / doc_absent / absent）必须各自
    钉死。用一份手造的迷你语料，不触网、不依赖 Dify。
    """
    corpus = {"documents": [
        {"name": "太保互联网个人一年期住院医疗保险条款",
         "segments": [{"position": 1, "content": "2.4 免赔额 一万元。"},
                      {"position": 4, "content": "3.1 犹豫期 十日。"}]},
        {"name": "太保团体终身重大疾病保险E款条款",
         "segments": [{"position": 2, "content": "1.4 犹豫期 二十日。"},
                      {"position": 3, "content": "2.4 免赔额 仅限重疾。"}]},
    ]}
    cases = [
        ("期望章节在期望文档里(med_ind)", {"product": "medical", "expected_clause": "2.4 免赔额"}, "present"),
        ("期望章节在期望文档里(ci)", {"product": "critical", "expected_clause": "1.4 犹豫期"}, "present"),
        # 编号+标题都在，但**只在 med_ind**：期望 critical 却只捞到医疗险那份 -> 串档
        ("编号标题都在但全来自别的产品", {"product": "critical", "expected_clause": "3.1 犹豫期"}, "wrong_doc"),
        # 同编号同标题在两份文档里都有，但期望的那份没有 -> 期望文档没这段
        ("标题命中但期望文档没有编号", {"product": "critical", "expected_clause": "2.4 免赔额.1 子条款"}, "absent"),
        ("全库都搜不到标题", {"product": "medical", "expected_clause": "9.9 不存在的章节"}, "absent"),
        ("没有期望章节", {"product": "medical", "expected_clause": None}, "no_anchor"),
    ]
    bad = []
    print("语料层自检 %d 条" % len(cases))
    for desc, case, want in cases:
        got = corpus_verdict(case, corpus)["verdict"]
        if got != want:
            bad.append("%-34s 期望 %-11s 实际 %s" % (desc, want, got))
        print("  %-34s %s" % (desc, got))

    for b in bad:
        print("  [x] " + b)
    if not bad:
        print("  [OK] 全部通过")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="t001")
    ap.add_argument("--cases", default=CASES)
    ap.add_argument("--only", default="", help="逗号分隔的用例 id")
    ap.add_argument("--resume", action="store_true", help="已采过的用例不重采")
    ap.add_argument("--top-k-cand", type=int, default=CANDIDATE_CAP)
    ap.add_argument("--live-threshold", type=float, default=None,
                    help="覆盖 C 段的 score_threshold（跑对照实验用）")
    ap.add_argument("--live-top-k", type=int, default=None,
                    help="覆盖 C 段的 top_k")
    ap.add_argument("--live-rerank", choices=["on", "off"], default=None,
                    help="覆盖 C 段的 reranking_enable")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑语料层判定自检（不触网、不读 corpus.json）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # C 段参数覆盖：只在真的传了参数时才建 override，
    # 否则传 None 让 C 段走线上快照原样（保持与生产同参）。
    live_override = None
    if (args.live_threshold is not None or args.live_top_k is not None
            or args.live_rerank is not None):
        live_override = {}
        if args.live_threshold is not None:
            live_override["score_threshold"] = args.live_threshold
        if args.live_top_k is not None:
            live_override["top_k"] = args.live_top_k
        if args.live_rerank is not None:
            live_override["reranking_enable"] = (args.live_rerank == "on")
        print("C 段参数覆盖（对照实验）：%s\n" % json.dumps(live_override, ensure_ascii=False))

    if not os.path.isfile(KEY_FILE):
        print("缺少 dataset token：%s" % KEY_FILE)
        return 2
    tok = io.open(KEY_FILE, encoding="utf-8").read().strip()

    corpus = load_corpus()
    if corpus is None:
        print("[!] 未找到 corpus.json —— 语料层判不了「库里有没有」。")
        print("    先跑：python scripts/dump_corpus.py")
        return 2

    with io.open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        cases = [c for c in cases if c["id"] in want]

    out_dir = os.path.join(RUNS, args.tag)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "trace.jsonl")

    done = set()
    if args.resume and os.path.isfile(out_path):
        with io.open(out_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:                                # noqa: BLE001
                    pass

    base = load_base()
    todo = [c for c in cases if c["id"] not in done]
    print("采集 %d 条（已采 %d 条跳过），每条 3 段探针，只检索不调 LLM\n"
          % (len(todo), len(done)))

    t0 = time.time()
    mode = "a" if (args.resume and os.path.isfile(out_path)) else "w"
    infra_hits = 0
    with io.open(out_path, mode, encoding="utf-8") as fout:
        for i, case in enumerate(todo, 1):
            rec = probe_case(tok, case, base, corpus, live_override)
            rec["live_override"] = live_override
            if rec.get("infra") == "rerank_unready":
                infra_hits += 1
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()          # 每条即时落盘：中断也不丢已跑的
            cv = rec["corpus"]["verdict"]
            st = rec["stages"]
            print("[%2d/%2d] %-6s 语料=%-12s A=%-3s B=%-3s C=%-3s 期望章节最佳排名 %s"
                  % (i, len(todo), case["id"], cv,
                     st.get("A_retrieval_only", {}).get("n", "-"),
                     st.get("B_reranked_full", {}).get("n", "-"),
                     st.get("C_live_params", {}).get("n", "-"),
                     st.get("C_live_params", {}).get("best_rank")))

    print("\n完成，用时 %.1f s -> %s" % (time.time() - t0, out_path))
    if infra_hits:
        print("\n[!] %d 条命中 infra=rerank_unready：A 段有结果但 B 段空，"
              "说明 TEI rerank 没就绪，不是召回问题。先等 TEI 加载完再采。"
              % infra_hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
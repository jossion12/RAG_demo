# -*- coding: utf-8 -*-
"""检索参数扫描：用**只检索不生成**的 API 做参数寻优。

为什么不直接改配置跑全量评测：一次全量评测在本机要 15 分钟左右，
而扫参数需要几十上百组。Dify 的 `/v1/datasets/{id}/retrieve` 直接收
retrieval_model，不进 LLM，一组几十条只要几秒——先用它把参数空间扫一遍，
再用全量评测验证入选配置。这样「调参」才是迭代而不是一次次赌 15 分钟。

判据与 run_iteration.py 对齐（同一个 DOC_PRODUCT / 章节号+标题两档命中），
但只看检索结果、不看 LLM 答案，所以它衡量的是**召回侧**：
  章节命中  期望章节的编号+标题都出现在召回片段里
  产品正确  召回片段所属文档与用例期望产品有交集
  目录噪声  前 3 条里出现「阅读指引/条款目录/释义」页
阈值往下调会同时涨「命中」和「噪声」，脚本把两条曲线一起打出来，
选的是**噪声可控前提下的最大命中**，不是单纯的最大召回。

用法：
    python scripts/sweep_retrieval.py --stage threshold
    python scripts/sweep_retrieval.py --stage knn --threshold 0.15
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))
import run_iteration as R                                    # noqa: E402

DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".dify_dataset_key")
OUT_DIR = os.path.join(ROOT, "eval", "runs")


def psql(sql):
    p = subprocess.run(["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres",
                        "-d", "dify", "-t", "-A", "-c", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
    return p.stdout.decode("utf-8").strip()


def base_config():
    return json.loads(psql("select retrieval_model from datasets where id='%s';" % DATASET_ID))


def retrieve(tok, query, rm):
    body = {"query": query, "retrieval_model": rm}
    req = urllib.request.Request(
        "http://localhost/v1/datasets/%s/retrieve" % DATASET_ID,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8"))
    out = []
    for x in d.get("records") or []:
        seg = x.get("segment") or {}
        out.append({"score": x.get("score") or 0.0,
                    "document": (seg.get("document") or {}).get("name", ""),
                    "content": seg.get("content") or "",
                    "position": seg.get("position")})
    return out


def probe_case(tok, case, rm):
    """跑一条用例的检索，返回可打分的中间结果。"""
    try:
        recs = retrieve(tok, case["question"], rm)
    except urllib.error.HTTPError as e:
        return {"id": case["id"], "error": "HTTP %s" % e.code, "n": 0}
    cites = [{"snippet": r["content"], "label": r["document"],
              "documentName": r["document"], "score": r["score"]} for r in recs]
    clause = case.get("expected_clause")
    hit = R.cite_hit(cites, clause) if clause else None
    allow = R.CASE_PRODUCTS.get(case.get("product"), set())
    prods = R.cited_products(cites)
    return {
        "id": case["id"], "error": None, "n": len(recs),
        "clause_hit": hit, "product_ok": bool(allow and prods and (prods & allow)),
        "cited_products": sorted(prods),
        "toc_noise": R.toc_noise(cites),
        "top_score": recs[0]["score"] if recs else 0.0,
        "scores": [round(r["score"], 4) for r in recs[:6]],
    }


def score_config(tok, cases, rm):
    rows = [probe_case(tok, c, rm) for c in cases]
    clause_rows = [r for r in rows if r["clause_hit"] is not None]
    ans = [r for r in rows if not cases_by_id[r["id"]].get("expect_refuse")]
    ref = [r for r in rows if cases_by_id[r["id"]].get("expect_refuse")]
    return {
        "rows": rows,
        "clause": (sum(1 for r in clause_rows if r["clause_hit"] == "exact"), len(clause_rows)),
        "title_only": sum(1 for r in clause_rows if r["clause_hit"] == "title_only"),
        "product_ok": (sum(1 for r in ans if r["product_ok"]), len(ans)),
        "noise": sum(1 for r in rows if r["toc_noise"] > 0),
        "avg_n": (sum(r["n"] for r in rows) / len(rows)) if rows else 0,
        "refuse_with_cites": sum(1 for r in ref if r["n"] > 0),
        "refuse_n": len(ref),
    }


def pct(a, b):
    return "%.0f%%" % (100.0 * a / b) if b else "-"


cases_by_id = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["threshold", "knn", "rerank", "custom"], default="threshold")
    ap.add_argument("--threshold", type=float, default=0.15)
    ap.add_argument("--top-k", type=int, default=30)
    ap.add_argument("--vector-weight", type=float, default=0.7)
    ap.add_argument("--keyword-weight", type=float, default=None)
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "retrieval_sweep.json"))
    args = ap.parse_args()

    if not os.path.isfile(KEY_FILE):
        print("缺少 dataset token：%s" % KEY_FILE)
        return 2
    tok = io.open(KEY_FILE, encoding="utf-8").read().strip()

    cases = json.load(io.open(os.path.join(ROOT, "eval", "eval_cases_v2.json"),
                              encoding="utf-8"))["cases"]
    global cases_by_id
    cases_by_id = {c["id"]: c for c in cases}

    grid = []
    if args.stage == "threshold":
        grid = [{"score_threshold": t} for t in (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4)]
    elif args.stage == "knn":
        kw = args.keyword_weight if args.keyword_weight is not None else 1 - args.vector_weight
        grid = []
        for k in (5, 10, 15, 20, 30):
            grid.append({"score_threshold": args.threshold, "top_k": k,
                         "_vw": args.vector_weight, "_kw": kw})
        for vw, kw in ((0.5, 0.5), (0.6, 0.4), (0.8, 0.2), (1.0, 0.0)):
            grid.append({"score_threshold": args.threshold, "top_k": args.top_k,
                         "_vw": vw, "_kw": kw})
    else:
        grid = [{"score_threshold": args.threshold, "top_k": args.top_k,
                 "_vw": args.vector_weight,
                 "_kw": args.keyword_weight if args.keyword_weight is not None else 1 - args.vector_weight}]

    # rerank 阶段回答两个问题：top_k 往上还有没有空间；
    # 以及「权重旋钮」到底是不是活的——若 rerank 开启时权重怎么调都没变化，
    # 要再在关闭 rerank 的前提下对照一次，才能区分「被 rerank 压制」和「旋钮没接上」。
    rerank_variants = [{"score_threshold": args.threshold, "top_k": k} for k in (30, 40, 60)]
    if args.stage == "rerank":
        for vw in (0.1, 0.5, 0.9):
            rerank_variants.append({"score_threshold": args.threshold, "top_k": args.top_k,
                                    "_vw": vw, "_kw": 1 - vw, "_rerank": True})
            rerank_variants.append({"score_threshold": args.threshold, "top_k": args.top_k,
                                    "_vw": vw, "_kw": 1 - vw, "_rerank": False})
        grid = rerank_variants

    base = base_config()
    results = []
    print("扫描 %d 组配置 × %d 条用例（只检索，不调 LLM）\n" % (len(grid), len(cases)))
    header = "%-34s %-12s %-9s %-11s %-8s %-9s" % ("配置", "章节命中", "仅标题", "产品正确", "目录噪声", "平均条数")
    print(header)
    print("-" * len(header))
    for g in grid:
        rm = json.loads(json.dumps(base))
        rm.update({"score_threshold": g["score_threshold"],
                   "top_k": g.get("top_k", base["top_k"])})
        if "_vw" in g:
            rm["weights"]["vector_setting"]["vector_weight"] = g["_vw"]
            rm["weights"]["keyword_setting"]["keyword_weight"] = g["_kw"]
        if "_rerank" in g:
            rm["reranking_enable"] = g["_rerank"]
        t0 = time.time()
        s = score_config(tok, cases, rm)
        label = "th=%.2f top_k=%-3d vw=%.1f kw=%.1f%s" % (
            g["score_threshold"], rm["top_k"],
            g.get("_vw", rm["weights"]["vector_setting"]["vector_weight"]),
            g.get("_kw", rm["weights"]["keyword_setting"]["keyword_weight"]),
            "" if "_rerank" not in g else (" rerank=ON" if g["_rerank"] else " rerank=OFF"))
        print("%-34s %-12s %-9d %-11s %-8d %-9.1f  (%.0fs)"
              % (label, "%d/%d %s" % (s["clause"][0], s["clause"][1], pct(*s["clause"])),
                 s["title_only"], "%s %s" % (pct(*s["product_ok"]), ""),
                 s["noise"], s["avg_n"], time.time() - t0))
        results.append({"config": g, "label": label,
                        "clause_hit": s["clause"][0], "clause_n": s["clause"][1],
                        "title_only": s["title_only"],
                        "product_ok": s["product_ok"][0], "product_n": s["product_ok"][1],
                        "noise": s["noise"], "avg_n": round(s["avg_n"], 2),
                        "refuse_with_cites": s["refuse_with_cites"], "refuse_n": s["refuse_n"],
                        "rows": s["rows"]})

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump({"stage": args.stage, "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "results": results}, f, ensure_ascii=False, indent=2)
    print("\n结果: %s" % args.out)

    best = max(results, key=lambda r: (r["clause_hit"] - 0.5 * r["noise"], -r["title_only"]))
    print("按「命中优先、噪声打对折」推荐: %s  (章节命中 %d/%d, 噪声 %d, 仅标题 %d)"
          % (best["label"], best["clause_hit"], best["clause_n"], best["noise"], best["title_only"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
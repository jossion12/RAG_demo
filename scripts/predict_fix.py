# -*- coding: utf-8 -*-
"""用扫描结果预测：阈值下调后，基线里那批失败应该修好哪些。

把 retrieval_sweep.json 里每个阈值的逐条检索结果，和 r001 基线的失败清单对齐，
给出「这次调参预期能修好 / 修不好」的预测。等 r002 全量评测出来后对账——
预测和实测对不上，说明调参之外还有别的变量在动，那本身就是重要信息。
"""
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))
import run_iteration as R                                              # noqa: E402

BASE_FAIL = ["C-04", "C-05", "C-07", "C-09", "C-10", "C-12", "G-01", "G-02", "G-03", "G-04",
             "G-05", "M-02", "M-04", "M-06", "M-09", "M-11", "M-12", "M-13", "M-15",
             "O-07", "O-08", "X-02"]
BASE_CLASS = {"C-04": "wrong_product", "C-05": "under_refusal", "C-07": "over_refusal",
              "C-09": "clause_mismatch", "C-10": "over_refusal", "C-12": "clause_mismatch",
              "G-01": "over_refusal", "G-02": "wrong_product", "G-03": "over_refusal",
              "G-04": "over_refusal", "G-05": "wrong_product", "M-02": "over_refusal",
              "M-04": "clause_mismatch", "M-06": "citation_miss", "M-09": "over_refusal",
              "M-11": "over_refusal", "M-12": "citation_miss", "M-13": "wrong_product",
              "M-15": "citation_miss", "O-07": "under_refusal", "O-08": "over_refusal",
              "X-02": "over_refusal"}


def load_sweep(path):
    d = json.load(io.open(path, encoding="utf-8"))
    out = {}
    for r in d["results"]:
        th = r["config"]["score_threshold"]
        out[th] = {row["id"]: row for row in r["rows"]}
    return out


def main():
    cases = {c["id"]: c for c in json.load(
        io.open(os.path.join(ROOT, "eval", "eval_cases_v2.json"), encoding="utf-8"))["cases"]}
    sweep = load_sweep(os.path.join(ROOT, "eval", "runs", "retrieval_sweep.json"))

    thresholds = sorted(sweep)
    base_th, new_th = max(thresholds), 0.10
    if new_th not in sweep:
        print("扫描结果里没有 %s 这一档" % new_th)
        return 1

    print("预测：阈值 %.2f -> %.2f，基线 %d 条失败中各条的可修复性\n" % (base_th, new_th, len(BASE_FAIL)))
    header = "%-6s %-15s %-28s %-28s %s" % ("id", "基线归因", "th=%.2f 检索" % base_th, "th=%.2f 检索" % new_th, "预测")
    print(header)
    print("-" * 115)

    fixable, notfixable = [], []
    for cid in BASE_FAIL:
        c = cases[cid]
        b = sweep[base_th][cid]
        n = sweep[new_th][cid]
        allow = R.CASE_PRODUCTS.get(c.get("product"), set())

        def desc(row):
            if row.get("error"):
                return "错误:" + row["error"][:14]
            ch = row.get("clause_hit")
            chs = {True: "章节命中", "exact": "章节命中", "title_only": "仅同名章节",
                   False: "未召回", None: "-"}.get(ch, str(ch))
            prod = "产品对" if row.get("product_ok") else "产品错"
            return "%s/%s/%d条" % (chs, prod, row.get("n", 0))

        # 可修复的判据：期望应答的题，召回里必须同时出现正确章节与正确产品。
        # 只满足其一不算——那是 clause_mismatch / wrong_product，阈值救不了。
        need = (not c.get("expect_refuse"))
        ok_clause = n.get("clause_hit") == "exact" or c.get("expected_clause") is None
        ok_prod = (not allow) or n.get("product_ok")
        if need and ok_clause and ok_prod:
            verdict, bucket = "可修复", fixable
        else:
            why = []
            if need and not ok_clause:
                why.append("章节未召回")
            if need and not ok_prod:
                why.append("产品不对")
            verdict, bucket = "调参救不了(%s)" % ",".join(why) or "不该出现", notfixable
        bucket.append(cid)
        print("%-6s %-15s %-28s %-28s %s" % (cid, BASE_CLASS.get(cid, "?"),
                                             desc(b), desc(n), verdict))

    print("\n预测可修复 %d 条: %s" % (len(fixable), " ".join(fixable)))
    print("预测调参无解 %d 条: %s" % (len(notfixable), " ".join(notfixable)))
    print("\n注：'调参救不了' 的题要么需要产品路由，要么需要清理知识库里的目录页——")
    print("    这两类都要靠架构或数据侧处理，不是 score_threshold 能解决的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
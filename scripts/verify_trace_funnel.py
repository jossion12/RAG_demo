# -*- coding: utf-8 -*-
"""守门：检索漏斗的三个阶段确实能被切开（否则 trace 归因全是假绿）。

为什么需要这个脚本：
    `run_iteration.py` 的归因是**端到端一层**。`citation_miss` / `over_refusal`
    只说明「引用没兜住期望条款」，但答不出来是卡在哪一环：
        索引里根本没这段（切割/入库）   -> 调阈值、换 rerank 都救不回来
        索引里有，向量召回没捞上来       -> 该调权重 / top_k / embedding
        捞上来了，rerank 把它压下去了     -> 该调 rerank 模型或阈值
        rerank 过了，被 score_threshold 砍 -> 该调阈值
    四类要用**不同的旋钮**去解，混在一个 passed=False 里只能靠猜。

    切开它们靠的是**对照探测**（同一个 query，只改一个旋钮）：
        A 关 rerank + 阈值 0  -> 混合检索的原始候选池（向量+关键词权重后的分）
        B 开 rerank + 阈值 0  -> rerank 之后的**全部分数**（没被阈值砍）
        C 现状参数           -> 真正进入 LLM 的那批

    前提是 B 真能拿到「阈值之前的 rerank 全量」。Dify 的
    `/v1/datasets/{id}/retrieve` 直接收 retrieval_model，这条路能走通；
    但如果哪天它开始按线上阈值过滤，B 就会退化成和 C 一样 —— 那时所有
    「rerank 压掉了 / 阈值砍掉了」的归因都会静默错判成同一类。

判据要按**多条 query** 判定，不能只看一条
------------------------------------------------
    阈值砍掉多少条是**逐 query 变化的**：某条问题的片段全在 0.5 分以上时，
    th=0.0 和 th=0.1 的结果本来就一样多条。只用一条 query 断言「两者必有差异」，
    健康环境下也会随机翻脸——而一个会误报的守门，久了就没人看它了。
    所以这里的规则是：**全部 query 都异常才算门禁失败**。

注意：top_k 硬上限 32。README 记的坑是「>=33 静默返回空集」，
    这里固定用 32 做候选池上限，别为了「多捞点」往上调。

用法：
    python scripts/verify_trace_funnel.py
    python scripts/verify_trace_funnel.py --query "住院医疗险的等待期是多少天?"
    python scripts/verify_trace_funnel.py --queries "问题甲|问题乙|问题丙"
"""
import argparse
import io
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".dify_dataset_key")
SNAPSHOT = os.path.join(ROOT, "eval", "params_snapshot.json")
CANDIDATE_CAP = 32          # 候选池上限；>=33 会静默返回空集

# 三条形态不同的问题：明确指向条款的、跨产品的、泛问的。
# 覆盖不同召回形态，避免守门只对某一类 query 成立。
DEFAULT_QUERIES = [
    "住院医疗险的等待期是多少天？",
    "重疾险的犹豫期是多久？退保扣多少钱？",
    "这款产品的年化收益率是多少？",
]

STAGES = [
    ("A_retrieval_only", {"reranking_enable": False, "score_threshold": 0.0, "top_k": CANDIDATE_CAP}),
    ("B_reranked_full",   {"reranking_enable": True,  "score_threshold": 0.0, "top_k": CANDIDATE_CAP}),
    ("C_live_params",    None),   # None = 用线上快照原样
]


def load_base():
    """基线配置取自 params_snapshot.json，不查库 —— 快照是已留档的线上同参。"""
    with io.open(SNAPSHOT, encoding="utf-8") as f:
        return json.load(f)["dataset_retrieval_model"]


def probe(tok, query, retrieval_model, timeout=180):
    """走 Dify 的只检索 API，返回该配置下**实际进入下一环**的片段列表。"""
    body = json.dumps({"query": query, "retrieval_model": retrieval_model}).encode("utf-8")
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
            "content": seg.get("content") or "",
        })
    return out


def stage_rows(tok, query):
    """跑三段探测，返回 {stage_name: [片段, ...]}。"""
    base = load_base()
    got = {}
    for name, override in STAGES:
        rm = json.loads(json.dumps(base))
        if override:
            rm.update(override)
        got[name] = probe(tok, query, rm)
    return got


def check_one(tok, query, show=6):
    """跑一条 query，返回 (fails, 漏斗条数三元组)。断言的是结构，不是召回准不准。"""
    got = stage_rows(tok, query)
    a, b, c = got["A_retrieval_only"], got["B_reranked_full"], got["C_live_params"]

    for name, rows in got.items():
        print("  %s：%d 条" % (name, len(rows)))
        for i, r in enumerate(rows[:show], 1):
            snippet = " ".join(r["content"].split())[:46]
            print("      %2d  score=%-9.4f pos=%-4s %-18s %s"
                  % (i, r["score"], r["pos"], r["doc"][:16], snippet))

    fails = []

    # 1. A/B 段必须能返回东西（全空说明 Dify 没起 / key 失效，属环境问题）。
    #    注意**不能**把「C 段空」也算异常：超出知识库范围的问题
    #    （「这款产品年化收益率多少」）正确拒答时 C 段本就该是 0 条。
    #    早先这里对三段一视同仁，结果 out_of_scope 查询每次都被报异常——
    #    一个天天误报的守门，久了就没人看它了。
    for name in ("A_retrieval_only", "B_reranked_full"):
        if not got[name]:
            fails.append("%s 返回空集 —— 无法区分「没召回」和「环境不通」" % name)
    if got["A_retrieval_only"] and not got["C_live_params"]:
        print("      （C 段为 0：对超出知识库范围的查询，这是**期望行为**，不计异常）")

    # 2. rerank 只重排不新增，A 的候选池必须覆盖 B
    if len(a) < len(b):
        fails.append("A(%d 条) < B(%d 条)：rerank 不应凭空造出候选，"
                     "候选池顺序或条数口径变了" % (len(a), len(b)))

    # 3. 阈值必须真的在砍东西，否则「阈值砍掉了」这一类归因永远不会被触发。
    #    注意这条是逐 query 的，泛问类 query 可能真的砍不动 —— 所以它只在
    #    「该 query 明显该被砍」时才有意义，外层用多条 query 做多数判定。
    if b and c and len(b) <= len(c):
        fails.append("B(th=0.0, %d 条) <= C(线上阈值, %d 条)："
                     "阈值没有可观测差异，无法用它归因"
                     % (len(b), len(c)))

    # 4. rerank 分数必须真的覆盖了检索分 —— 量纲不同是「rerank 生效」的证据。
    #    若两段分数逐条相同，说明 rerank 没接上（或被原样透传），
    #    此时「rerank 压掉了」与「检索没捞到」无法区分。
    if a and b:
        a_top, b_top = a[0]["score"], b[0]["score"]
        if abs(a_top - b_top) < 1e-6 and len(a) == len(b):
            fails.append("A/B 首条分相同且条数相同（%.4f）：rerank 可能未生效，"
                         "A/B 对照会退化成同一个观测点" % a_top)
        else:
            print("      量纲对照：A 首条 %.4f（检索分） vs B 首条 %.4f（rerank 分）"
                  % (a_top, b_top))

    return fails, (len(a), len(b), len(c))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="", help="单条 query（只用这一条判定）")
    ap.add_argument("--queries", default="", help="多条 query，用 | 分隔")
    ap.add_argument("--show", type=int, default=6, help="每段打印前 N 条，0 为不打印")
    args = ap.parse_args()

    if not os.path.isfile(KEY_FILE):
        print("缺少 dataset token：%s" % KEY_FILE)
        return 2
    tok = io.open(KEY_FILE, encoding="utf-8").read().strip()

    if args.query:
        queries = [args.query]
    elif args.queries:
        queries = [s.strip() for s in args.queries.split("|") if s.strip()]
    else:
        queries = DEFAULT_QUERIES

    broken = []
    print("探测 %d 条 query（全部异常才判门禁失败）\n" % len(queries))
    for q in queries:
        print("=" * 92)
        print("Q: %s" % q)
        print("=" * 92)
        try:
            fails, counts = check_one(tok, q, args.show)
        except Exception as e:                                # noqa: BLE001
            print("探测失败（先确认 Dify 已启动、TEI 已就绪）：%s: %s"
                  % (type(e).__name__, e))
            broken.append((q, ["探测异常：%s" % type(e).__name__]))
            print()
            continue
        if fails:
            broken.append((q, fails))
        print("  -> 漏斗：候选池 %d -> rerank %d -> 过阈值 %d；%s\n"
              % (counts[0], counts[1], counts[2],
                 "异常" if fails else "正常"))

    if broken and len(broken) < len(queries):
        print("=%d 条 query 异常，%d 条正常 —— 门禁通过。\n"
              % (len(broken), len(queries) - len(broken)))
        for q, fails in broken:
            print("  [!] %s" % q)
            for f in fails:
                print("      - %s" % f)
        return 0

    if broken:
        print("漏斗结构不成立，trace 归因会静默错判：")
        for q, fails in broken:
            print("  [x] %s" % q)
            for f in fails:
                print("      - %s" % f)
        return 1

    print("[OK] 全部 %d 条 query 的漏斗三段均可切开" % len(queries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
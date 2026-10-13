# -*- coding: utf-8 -*-
"""知识库语料快照：把「索引里到底存了什么」拉成可离线判定的文件。

为什么需要它：
    要判断「没召回」是不是切割/入库造成的，前提是能回答一个问题——
    **期望的条款内容，此刻在不在知识库里？**
    这个问题在检索侧永远答不出来：检索没捞到，不等于库里没有。
    只有把全库片段原样拉下来做文本匹配，才能把
        「库里就没有（切割/入库问题，调阈值和 rerank 都救不回来）」
    和
        「库里有、只是没捞上来（召回问题）」
    分开。这两类要用完全不同的旋钮去解，混在一起就只能靠猜。

    附带产出**切割质量诊断**（长度分布、超长段、疑似未切分的整页），
    这是唯一能直接看到「切得怎么样」的地方——检索指标全都在下游。

数据来源：Dify 的只读 dataset API，宿主机可直接访问，无需进容器。
    GET /v1/datasets/{id}/documents            -> 文档清单
    GET /v1/datasets/{id}/documents/{doc}/segments -> 该文档的全部分段（分页）

用法：
    python scripts/dump_corpus.py                 # 落 eval/runs/corpus.json
    python scripts/dump_corpus.py --out x.json    # 指定落盘路径
    python scripts/dump_corpus.py --stats-only    # 只看诊断，不落盘
"""
import argparse
import io
import json
import os
import statistics
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".dify_dataset_key")
DIFY = "http://localhost/v1/datasets/" + DATASET_ID

DEFAULT_OUT = os.path.join(ROOT, "eval", "runs", "corpus.json")

PAGE = 100


def _get(tok, url, timeout=60):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_documents(tok):
    docs, page = [], 1
    while True:
        d = _get(tok, "%s/documents?page=%d&limit=%d" % (DIFY, page, PAGE))
        docs.extend(d.get("data") or [])
        if not d.get("has_more"):
            return docs
        page += 1


def fetch_segments(tok, doc_id):
    segs, page = [], 1
    while True:
        d = _get(tok, "%s/documents/%s/segments?page=%d&limit=%d" % (DIFY, doc_id, page, PAGE))
        segs.extend(d.get("data") or [])
        if not d.get("has_more"):
            return segs
        page += 1


def norm(s):
    """全角空白/换行差异会让纯文本匹配假阴性，统一压掉。"""
    import re
    return re.sub(r"\s+", "", s or "")


def diagnose(docs):
    """切割质量诊断。只报**结构性**异常，不做内容质量判断。"""
    rows = []
    for doc in docs:
        lens = [len(norm(s["content"])) for s in doc["segments"]]
        if not lens:
            rows.append({"doc": doc["name"], "segments": 0})
            continue
        longs = [l for l in lens if l >= 1500]
        rows.append({
            "doc": doc["name"],
            "segments": len(lens),
            "chars_min": min(lens),
            "chars_median": int(statistics.median(lens)),
            "chars_max": max(lens),
            # 超长段通常意味着「整页没切开」：目录页、正文首页、
            # 或者分段符把一整节并进了一个片段。它们会把精确条款埋掉。
            "oversize_segments": len(longs),
            "oversize_positions": [doc["segments"][i]["position"]
                                   for i, l in enumerate(lens) if l >= 1500],
            "disabled_segments": sum(1 for s in doc["segments"] if not s.get("enabled", True)),
            "not_completed": sum(1 for s in doc["segments"]
                                 if (s.get("status") or "completed") != "completed"),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--stats-only", action="store_true", help="只打印诊断，不落盘")
    args = ap.parse_args()

    if not os.path.isfile(KEY_FILE):
        print("缺少 dataset token：%s" % KEY_FILE)
        return 2
    tok = io.open(KEY_FILE, encoding="utf-8").read().strip()

    try:
        raw_docs = fetch_documents(tok)
    except Exception as e:                                        # noqa: BLE001
        print("拉取文档清单失败（先确认 Dify 已启动）：%s: %s" % (type(e).__name__, e))
        return 2

    docs = []
    for d in raw_docs:
        segs = fetch_segments(tok, d["id"])
        docs.append({
            "id": d["id"],
            "name": d["name"],
            "word_count": d.get("word_count"),
            "segments": [{
                "position": s.get("position"),
                "word_count": s.get("word_count"),
                "enabled": s.get("enabled", True),
                "status": s.get("status"),
                "content": s.get("content") or "",
            } for s in segs],
        })

    print("文档 %d 份，片段 %d 个\n" % (len(docs), sum(len(d["segments"]) for d in docs)))
    print("%-34s %-7s %-7s %-7s %-7s %s" % ("文档", "段数", "最短", "中位", "最长", "超长段(>=1500字)"))
    print("-" * 92)
    for r in diagnose(docs):
        if not r.get("segments"):
            print("%-34s %d（空）" % (r["doc"][:32], 0))
            continue
        flag = " <- %s" % r["oversize_positions"] if r["oversize_positions"] else ""
        print("%-34s %-7d %-7d %-7d %-7d %d%s"
              % (r["doc"][:32], r["segments"], r["chars_min"], r["chars_median"],
                 r["chars_max"], r["oversize_segments"], flag))
        if r["disabled_segments"]:
            print("    [!] %d 个片段被禁用" % r["disabled_segments"])
        if r["not_completed"]:
            print("    [!] %d 个片段索引未完成" % r["not_completed"])

    if args.stats_only:
        return 0

    payload = {"dataset_id": DATASET_ID,
               "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "documents": docs}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("\n语料快照 -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
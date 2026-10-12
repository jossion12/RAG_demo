# -*- coding: utf-8 -*-
"""用裁掉目录页的 PDF（data/source/noc/）替换 Dify 知识库里的旧文档。

为什么需要它：eval/runs/r004_prompt/report.md 记录「⚠ 目录页噪声 18/52 = 35%」——
封面/阅读指引/条款目录页对「犹豫期/等待期/责任免除」这类词面命中极高，
会把正文条款挤出候选。docs/数据源验证报告.md 已写明「必须区分目录条目与
正文条款」。docs/评测迭代报告.md 的结论是「调参救不了，必须清理知识库」，
所以这里从数据侧动手：裁页 -> 重灌，而不是继续调 top_k/threshold。

安全设计：
  * --apply 之前只 dry-run 打印计划
  * 默认只重建「知识库里已有」的那两份文档；--all 会连团体百万医疗一起灌
    （那份目前**不在**知识库里，单独加会改变本轮评测口径，故默认不加）
  * 旧文档 id 落盘到 eval/runs/kb_doc_swap.json，可按 id 手工回滚

用法：
    python scripts/reingest_kb.py            # dry-run
    python scripts/reingest_kb.py --apply    # 真删真传
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOC = os.path.join(ROOT, "data", "source", "noc")
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".dify_dataset_key")
BASE = "http://localhost/v1/datasets/%s" % DATASET_ID
DB = "docker-db_postgres-1"
SWAP_LOG = os.path.join(ROOT, "eval", "runs", "kb_doc_swap.json")

import requests  # noqa: E402


def psql(sql):
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
    return p.stdout.decode("utf-8").strip()


def current_docs():
    raw = psql("select id, name, indexing_status, word_count from documents "
               "where dataset_id='%s' and enabled=true order by name;" % DATASET_ID)
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        did, name, status, wc = (line.split("|") + ["", "", "", ""])[:4]
        out.append({"id": did, "name": name, "status": status, "word_count": int(wc or 0)})
    return out


def api(method, path, **kw):
    tok = open(KEY_FILE, encoding="utf-8").read().strip()
    r = requests.request(method, BASE + path, headers={"Authorization": "Bearer " + tok},
                         timeout=300, **kw)
    if r.status_code >= 400:
        raise RuntimeError("HTTP %d %s" % (r.status_code, r.text[:300]))
    # DELETE 返回 204 空响应体，强行 r.json() 会抛 JSONDecodeError
    return r.json() if r.content.strip() else {}


def upload_doc(path):
    """单份上传。

    Dify 1.17 已废弃 /document/batches 系列（document.py:698 明确让调用方
    改用 create-by-file），所以这里直接走单文件端点。
    doc_form 必须显式给 hierarchical_model —— 知识库本身就是父分段模式
    （datasets.doc_form），不传会退回 text 模式，切分口径与旧基线不一致。
    """
    data = {
        "indexing_technique": "high_quality",
        "doc_form": "hierarchical_model",
        "doc_language": "zh",
        "embedding_model": "bge-m3:latest",
        "embedding_model_provider": "langgenius/ollama/ollama",
    }
    with open(path, "rb") as fh:
        return api("POST", "/document/create-by-file", data=data,
                   files={"file": (os.path.basename(path), fh, "application/pdf")})


def wait_index(doc_ids, timeout=1800):
    """轮询 documents.indexing_status 到 completed / error。"""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        rows = []
        for did in doc_ids:
            d = api("GET", "/documents/%s" % did)
            rows.append((did[:8], d.get("indexing_status"), d.get("word_count")))
        key = tuple(rows)
        if key != last:
            for did8, st, wc in rows:
                print("    %s %s word_count=%s" % (did8, st, wc))
            last = key
        if all(st in ("completed", "error") for _, st, _ in rows):
            return {did8: st for did8, st, _ in rows}
        time.sleep(6)
    raise TimeoutError("索引超时")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="连知识库里不存在的团体百万医疗一起灌（会改变评测口径）")
    args = ap.parse_args()

    docs = current_docs()
    print("知识库现有文档 %d 份：" % len(docs))
    for d in docs:
        print("  %s  %-46s %s  %d字" % (d["id"][:8], d["name"], d["status"], d["word_count"]))

    have = {d["name"] for d in docs}
    plan = []
    for name in sorted(os.listdir(NOC)):
        if not name.lower().endswith(".pdf"):
            continue
        if name not in have and not args.all:
            # 知识库原本就只有 2 份，团体百万医疗不在其中；补它会改变评测口径。
            # 但知识库被清空（docs 为空）时，它和另两份一样都该回来，否则知识库
            # 会比基线少文档 —— 那是「修复」引入的新偏差。
            print("\n跳过 %s（知识库里没有；加 --all 才会灌，会改变评测口径）" % name)
            continue
        plan.append(name)

    print("\n计划替换 %d 份：" % len(plan))
    for name in plan:
        src = os.path.join(NOC, name)
        print("  %-46s %8d bytes" % (name, os.path.getsize(src)))
    if not args.apply:
        print("\ndry-run，加 --apply 执行。")
        return

    swapped = []
    for d in docs:
        if d["name"] in plan:
            print("\n[删] %s (%s)" % (d["name"], d["id"][:8]))
            api("DELETE", "/documents/%s" % d["id"])
            swapped.append({"old_id": d["id"], "name": d["name"],
                            "old_word_count": d["word_count"]})

    new_ids = []
    for name in plan:
        src = os.path.join(NOC, name)
        print("\n[传] %s" % name)
        d = upload_doc(src)["document"]
        new_ids.append(d["id"])
        print("  -> %s" % d["id"])
        for e in swapped:
            if e["name"] == name:
                e["new_id"] = d["id"]

    print("\n[等索引]")
    status = wait_index(new_ids)
    bad = [k for k, v in status.items() if v != "completed"]
    for e in swapped:
        print("  %-46s %s -> %s" % (e["name"], e["old_id"][:8],
                                    e.get("new_id", "?")[:8]))
    for name, did in zip(plan, new_ids):
        if not any(e["name"] == name for e in swapped):
            print("  %-46s (新增) -> %s" % (name, did[:8]))

    with open(SWAP_LOG, "w", encoding="utf-8") as fh:
        json.dump({"dataset_id": DATASET_ID, "swapped": swapped,
                   "index_status": status}, fh, ensure_ascii=False, indent=2)
    print("\n换档记录已写 %s" % SWAP_LOG)
    if bad:
        print("[!] 索引未完成: %s" % bad)
        sys.exit(1)


if __name__ == "__main__":
    main()

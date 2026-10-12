"""检查当前草稿（draft）是否仍保有修复。

时间线（本机实测）：
  12:24 UTC  fix_workflow_graph.py --apply 写入修复
  13:00 UTC  用户发布 -> 产生版本 2
  13:01 UTC  draft updated_at 又变了
若 draft 内容已被覆盖，说明用户在未硬刷新的页面上保存过。
"""

import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def psql(sql: str) -> str:
    p = subprocess.run(
        ["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
         "-t", "-A", "-c", sql],
        capture_output=True,
    )
    return p.stdout.decode("utf-8").strip() if p.returncode == 0 else f"[err] {p.stderr.decode('utf-8','replace')[:200]}"


updated = psql("select updated_at from workflows where version='draft';")
raw = psql("select graph from workflows where version='draft';")
g = json.loads(raw)

print(f"draft updated_at = {updated}")
print(f"节点数 = {len(g['nodes'])}   连线数 = {len(g['edges'])}")
print()

has_fixer = any(n["data"]["type"] == "parameter-extractor" for n in g["nodes"])
llm = next(n["data"] for n in g["nodes"] if n["data"]["type"] == "llm")
kr = next(n["data"] for n in g["nodes"] if n["data"]["type"] == "knowledge-retrieval")
answer = next(n["data"] for n in g["nodes"] if n["data"]["type"] == "answer")
cfg = kr.get("multiple_retrieval_config") or {}

rows = [
    ("参数提取器节点已删除", not has_fixer),
    ("dataset_ids 只剩 1 个", len(kr.get("dataset_ids") or []) == 1, kr.get("dataset_ids")),
    ("score_threshold = 0.5", cfg.get("score_threshold") == 0.5, cfg.get("score_threshold")),
    ("top_k = 8", cfg.get("top_k") == 8, cfg.get("top_k")),
    ("提示词含拒答约束", "未找到相关信息" in llm["prompt_template"][0]["text"]),
    ("回答节点直连 LLM", "text#" in (answer.get("answer") or ""), answer.get("answer")),
]
for label, passed, actual in [(r[0], r[1], r[2] if len(r) > 2 else None) for r in rows]:
    print(f"[{'PASS' if passed else 'FAIL'}] {label}" + (f"  ->  {actual}" if actual is not None else ""))

print("\n结论: " + ("draft 仍保有修复（但已发布版本是旧的）" if all(r[1] for r in rows)
                    else "draft 已被覆盖，修复丢失"))

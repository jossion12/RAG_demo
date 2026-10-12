"""检查已发布版本的图是否包含修复，并输出节点级检索配置。

用法：python scripts/check_published_workflow.py
"""

import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SQL = """
select version, version_number,
       (graph::jsonb #> '{nodes}')::text
from workflows
where version <> 'draft'
order by version_number;
"""


def psql_rows(sql: str):
    p = subprocess.run(
        ["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
         "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True,
    )
    if p.returncode != 0:
        print("[x] psql:", p.stderr.decode("utf-8", "replace")[:300])
        return []
    out = p.stdout.decode("utf-8").strip()
    return [line.split("|", 2) for line in out.splitlines() if line.strip()]


rows = psql_rows(SQL)
print(f"已发布版本数: {len(rows)}\n")

for ver, vnum, nodes_json in rows:
    nodes = json.loads(nodes_json)
    print("=" * 70)
    print(f"version={vnum}  ({ver})")
    print("=" * 70)
    for n in nodes:
        d = n["data"]
        print(f"  {d['type']}", end="")
        if d["type"] == "knowledge-retrieval":
            cfg = d.get("multiple_retrieval_config") or {}
            rm = cfg.get("reranking_model") or {}
            print(f"\n      dataset_ids = {d.get('dataset_ids')}")
            print(f"      score_threshold = {cfg.get('score_threshold')!r}   <== 必须不是 None")
            print(f"      top_k = {cfg.get('top_k')}")
            print(f"      reranking_enable = {cfg.get('reranking_enable')}")
            print(f"      reranking_model = {rm.get('model')}")
        elif d["type"] == "llm":
            txt = d["prompt_template"][0]["text"]
            print(f"\n      提示词含拒答约束 = {'未找到相关信息' in txt}")
            print(f"      记忆变量关闭 = {d.get('memory', {}).get('window', {}).get('enabled') is False}")
        elif d["type"] == "answer":
            print(f"\n      answer = {d.get('answer')}")
        else:
            print()
    print()

print("=" * 70)
print("weaviate 集合")
print("=" * 70)
p = subprocess.run(
    ["docker", "exec", "docker-weaviate-1", "curl", "-s", "http://localhost:8080/v1/schema"],
    capture_output=True,
)
if p.returncode == 0:
    try:
        sch = json.loads(p.stdout.decode("utf-8"))
        for c in sch.get("classes", []):
            print(f"  class = {c.get('class')}  vectorCount={c.get('vectorIndexConfig', {}).get('vectorIndexType')}")
            props = [pr["name"] for pr in c.get("properties", [])]
            print(f"     properties = {props}")
    except Exception as e:  # noqa: BLE001
        print("  解析失败:", e, p.stdout[:200])
else:
    print("  [x] 无法查询 weaviate schema:", p.stderr.decode("utf-8", "replace")[:200])

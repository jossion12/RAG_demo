"""查看某次运行的节点输出（避开 PowerShell 引号地狱）。"""

import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NODE_ID = sys.argv[1] if len(sys.argv) > 1 else "3ce67a15-c670-4a9e-882e-c1518620668c"

p = subprocess.run(
    ["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
     "-t", "-A", "-c", f"select outputs from workflow_node_executions where id='{NODE_ID}';"],
    capture_output=True,
)
if p.returncode != 0:
    print("[x]", p.stderr.decode("utf-8", "replace")[:300])
    sys.exit(2)

d = json.loads(p.stdout.decode("utf-8"))
res = d.get("result") or []
print(f"检索条数: {len(res)}")
for i, x in enumerate(res, 1):
    m = x.get("metadata") or {}
    print(f"[{i}] score={m.get('score')}  doc={m.get('document_name')}  "
          f"seg_pos={m.get('segment_position')}  position={m.get('position')}")
    print("    " + (x.get("content") or "")[:120].replace("\n", " "))
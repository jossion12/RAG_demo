"""后端存活探针：一条问答，打印答案/引用/耗时。

用法：
    C:\\Python312\\python.exe scripts\\ping_backend.py "重疾险的犹豫期是多久？"
"""

import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

q = sys.argv[1] if len(sys.argv) > 1 else "重疾险的犹豫期是多久？"
t = time.time()
req = urllib.request.Request(
    "http://localhost:8080/api/chat",
    data=json.dumps({"question": q}, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode("utf-8"))

ans = d.get("answer") or ""
print(f"[{time.time() - t:.2f}s] 答案: {ans[:120]}")
print(f"  引用 {len(d.get('citations') or [])} 条  refused={d.get('refused')}  "
      f"elapsedMs={d.get('elapsedMs')}")
for i, c in enumerate((d.get("citations") or [])[:3], 1):
    print(f"    [{i}] {c.get('documentName')}  score={c.get('score')}")
sys.exit(0 if ans.strip() else 1)
"""单问题快速探针：确认工作流修好后是否真能答出 + 是否带引用。

用法：
    C:\\Python312\\python.exe scripts\\quick_probe.py "住院医疗险的等待期是多久？"

Key 从 DB 读入内存，全程不打印明文。
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost/v1/chat-messages"
TOKEN_ID_PREFIX = "21e9da6b"


def get_key() -> str:
    p = subprocess.run(
        ["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
         "-t", "-A", "-c",
         f"select token from api_tokens where id::text like '{TOKEN_ID_PREFIX}%' "
         f"and app_id='f5939e3a-5fd7-43e7-8824-54d6ed1bc8da';"],
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
    k = p.stdout.decode("utf-8").strip()
    if not k:
        raise RuntimeError("api_tokens 里没查到 token")
    return k


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "住院医疗险的等待期是多久？"
    key = get_key()
    print(f"[*] key 前缀 {key[:8]}***  已读入内存（不打印明文）")

    body = {"inputs": {}, "query": query, "response_mode": "blocking", "user": "probe-user"}
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[x] HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        return 2
    dt = time.time() - t0

    ans = data.get("answer") or ""
    meta = data.get("metadata") or {}
    res = meta.get("retriever_resources") or []

    print(f"[*] 耗时 {dt:.2f}s")
    print(f"[*] answer 长度 = {len(ans)}")
    print(f"[*] status = {data.get('status')}")
    print(f"\n--- 答案 ---\n{ans[:1200]}\n")

    print(f"--- 引用 retriever_resources: {len(res)} 条 ---")
    for i, r in enumerate(res, 1):
        seg = (r.get("segment") or {}).get("content", "")
        doc = (r.get("document") or {}).get("name", "")
        print(f"[{i}] score={r.get('score')} pos={r.get('position')} doc={str(doc)[:30]}")
        print(f"    {seg[:90].replace(chr(10), ' ')}")

    usage = meta.get("usage") or {}
    if usage:
        print(f"\n--- usage: {usage}")

    ok = len(ans.strip()) > 0 and len(res) > 0
    print(f"\n结论: {'正常（有答案且有引用）' if ok else '异常'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
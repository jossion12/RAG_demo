"""SSE 流式探针：看流式模式是否能拿到 retriever_resources。

为什么单独试流式：blocking 与 streaming 的 metadata 组装是两条不同代码路径
（generate_task_pipeline 的 _handle_retriever_resources_event 在两边都会被调，
但 metadata 落库的时机不同）。如果流式能吐引用，Java 侧改走流式即可，
不用去改 Dify 内部。

用法：
    C:\\Python312\\python.exe scripts\\quick_probe_stream.py
"""

import json
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost/v1/chat-messages"
TOKEN_PREFIX = "21e9da6b"
APP_ID = "f5939e3a-5fd7-43e7-8824-54d6ed1bc8da"


def get_key() -> str:
    p = subprocess.run(
        ["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
         "-t", "-A", "-c",
         f"select token from api_tokens where id::text like '{TOKEN_PREFIX}%' and app_id='{APP_ID}';"],
        capture_output=True,
    )
    k = p.stdout.decode("utf-8").strip()
    if not k:
        raise RuntimeError("没查到 api token")
    return k


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "住院医疗险的等待期是多久？"
    key = get_key()
    print(f"[*] key 前缀 {key[:8]}***（不打印明文）")

    body = {"inputs": {}, "query": query, "response_mode": "streaming", "user": "probe-stream"}
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )

    t0 = time.time()
    answer = ""
    first_tok = None
    events = []
    meta = {}
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            et = ev.get("event")
            events.append(et)
            if et == "message":
                chunk = ev.get("answer") or ""
                if chunk and first_tok is None:
                    first_tok = time.time() - t0
                answer += chunk
            if et in ("message_end", "message_file_end"):
                meta = ev.get("metadata") or meta
            if "retriever" in json.dumps(ev, ensure_ascii=False).lower():
                print(f"    <含 retriever 字样> event={et}")

    total = time.time() - t0
    res = meta.get("retriever_resources") or []
    print(f"\n[*] 首token {first_tok:.2f}s / 总 {total:.2f}s" if first_tok else f"\n[*] 总 {total:.2f}s")
    print(f"[*] 事件序列: {events}")
    print(f"[*] 答案: {answer[:200]}")
    print(f"[*] metadata 键: {list(meta.keys())}")
    print(f"\n--- 引用 retriever_resources: {len(res)} 条 ---")
    for i, r in enumerate(res, 1):
        print(f"[{i}] score={r.get('score')} doc={str(r.get('document_name'))[:34]}")
        print(f"    {(r.get('content') or '')[:90].replace(chr(10), ' ')}")
    return 0 if res else 1


if __name__ == "__main__":
    sys.exit(main())
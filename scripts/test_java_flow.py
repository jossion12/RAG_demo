"""端到端跑通 Java 治理层全流程。

覆盖：
  POST /api/chat            阻塞问答（过滤 -> Dify -> 引用组装 -> 落库）
  POST /api/chat/stream     SSE 流式（delta -> citations -> done）
  GET  /api/chat/history    会话历史（验证审计落库 FR-07）

用法：python scripts/test_java_flow.py
"""

import json
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost:8080"
TIMEOUT = 240


def post_json(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def blocking(question):
    t0 = time.time()
    data = post_json("/api/chat", {"question": question})
    return data, time.time() - t0


def stream(question):
    """读 SSE：delta 累积正文，citations/done 收尾。

    Spring 的 SseEmitter.event().name(x).data(y) 产出的报文是
        event:delta
        data:<y>
    也就是说【事件名在 event: 行，不在 JSON 里】，而且 data 的形态随事件而变：
        delta     -> 裸字符串（不是 JSON，直接 json.loads 会失败）
        citations -> JSON 数组字符串
        done      -> JSON 对象字符串
    早先的解析器只读 data 行、并假设 data 里带 event 字段，
    于是 delta 被当成坏 JSON 丢弃（正文恒空）、citations 被当成 dict 取 .get
    抛 AttributeError: 'list' object has no attribute 'get'。
    """
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=json.dumps({"question": question}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events = []
    text = []
    first = None
    t0 = time.time()
    ev_name = None
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
            if line.startswith("event:"):
                ev_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            if body == "":
                continue
            # delta 是裸文本，其余是 JSON
            payload = body
            if ev_name != "delta":
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = body
            events.append({"event": ev_name, "data": payload})
            if ev_name == "delta" and isinstance(payload, str) and payload:
                if first is None:
                    first = time.time() - t0
                text.append(payload)
            elif ev_name in ("citations", "done") and first is None:
                first = time.time() - t0
    return events, "".join(text), first or 0.0, time.time() - t0


def show(label, data, secs):
    print(f"--- {label}   耗时 {secs:.2f}s")
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                print(f"    {k}: {len(v)} 项")
                for i, item in enumerate(v[:3], 1):
                    if isinstance(item, dict):
                        brief = {kk: item.get(kk) for kk in
                                 ("documentName", "segmentPosition", "pageNumber", "score", "snippet")
                                 if kk in item}
                        print(f"      [{i}] {json.dumps(brief, ensure_ascii=False)[:200]}")
                    else:
                        print(f"      [{i}] {str(item)[:120]}")
            else:
                sv = str(v)
                print(f"    {k}: {sv[:300]}")
    print()


def main() -> int:
    print("=" * 78)
    print("① 阻塞问答 —— 应答类")
    print("=" * 78)
    for q in ["住院医疗险的等待期是多久？", "重疾险的犹豫期是多久？退保扣多少钱？"]:
        try:
            d, s = blocking(q)
            show(q, d, s)
        except Exception as e:  # noqa: BLE001
            print(f"--- {q}\n    ERR {type(e).__name__}: {str(e)[:200]}\n")

    print("=" * 78)
    print("② 阻塞问答 —— 库外拒答类")
    print("=" * 78)
    for q in ["这款产品的年化收益率是多少？"]:
        try:
            d, s = blocking(q)
            show(q, d, s)
        except Exception as e:  # noqa: BLE001
            print(f"--- {q}\n    ERR {type(e).__name__}: {str(e)[:200]}\n")

    print("=" * 78)
    print("③ 敏感过滤（FR-08）：输入含身份证号")
    print("=" * 78)
    try:
        d, s = blocking("我的身份证是110101199003078515，等待期是多久？")
        show("身份证入参", d, s)
    except Exception as e:  # noqa: BLE001
        print(f"    ERR {type(e).__name__}: {str(e)[:200]}\n")

    print("=" * 78)
    print("④ SSE 流式")
    print("=" * 78)
    try:
        events, text, first, total = stream("投保后多久内身故赔钱？")
        print(f"    事件序列: {[e.get('event') for e in events]}")
        print(f"    首事件 {first:.2f}s / 总 {total:.2f}s")
        print(f"    正文({len(text)} 字): {text[:200]}")
        for e in events:
            if e.get("event") in ("citations", "done"):
                print(f"    <{e['event']}> {json.dumps(e['data'], ensure_ascii=False)[:400]}")
    except Exception as e:  # noqa: BLE001
        print(f"    ERR {type(e).__name__}: {str(e)[:200]}")
    print()

    print("=" * 78)
    print("⑤ 会话历史（FR-07 审计落库）")
    print("=" * 78)
    try:
        hist = get_json("/api/chat/history")
        print(f"    共 {len(hist)} 条")
        for h in hist[-3:]:
            print(f"    - {(h.get('question') or '')[:34]}  ->  "
                  f"{(h.get('answer') or '')[:40]}  refused={h.get('refused')}")
    except Exception as e:  # noqa: BLE001
        print(f"    ERR {type(e).__name__}: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

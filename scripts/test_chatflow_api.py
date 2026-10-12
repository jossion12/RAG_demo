"""端到端验证 Dify Chatflow：应答类问题 + 库外拒答类问题。

用法（拿到 API Key 后）：
    & "C:\\Python312\\python.exe" scripts\\test_chatflow_api.py --key app-xxxxxxxx
    & "C:\\Python312\\python.exe" scripts\\test_chatflow_api.py --key app-xxxxxxxx --stream

对照需求文档：
    AC-02 应答类覆盖率 ≥85%，关键数字（30日/1万元/15日）无错
    AC-03 引用命中率 ≥90%，每条引用可回看原文片段
    AC-04 库外类全部拒答，误拒率 ≤10%
    FR-06  Java 侧从 metadata.retriever_resources 组装 Citation

注意：脚本只把 Key 传给 Dify，不落盘、不打印。
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP_ID = 'f5939e3a-5fd7-43e7-8824-54d6ed1bc8da'
BASE = "http://localhost/v1/chat-messages"

# 应答类：期望答得出，且答案里应出现这些关键数字
ANSWER_CASES = [
    ("投保后多久内身故赔钱？", ["90日", "90 日", "90天"]),
    ("住院医疗险的等待期是多久？", ["30日", "30 日"]),
    ("重疾险的犹豫期是多久？退保扣多少钱？", ["15日", "15 日", "工本费"]),
    ("住院医疗保险的一般医疗免赔额是多少？", ["1万", "1 万元", "一万"]),
    ("怎么申请理赔，需要带什么材料？", ["病理", "诊断证明"]),
]

# 库外类：期望拒答（写成 (问题, []) 保持与 ANSWER_CASES 同样的二元组形状，
# 否则下面 for query, keys in cases 会抛 too many values to unpack）
#
# 注意：「怎么申请理赔，需要带什么材料」原本放在这里，实测是【用例归类错误】——
# 条款 3.4 明确写了病理报告/诊断证明/病历报告，LLM 答出来是正确行为，
# 已移到 ANSWER_CASES。
REFUSE_CASES = [
    ("这款产品的年化收益率是多少？", []),
    ("这个保险能贷款吗？", []),
    ("公司电话是多少？", []),
    ("明天上海天气怎么样？", []),
]

REFUSE_MARKERS = ["未找到相关信息", "未找到", "没有找到", "无法找到", "知识库中未"]


def call(key: str, query: str, user: str, stream: bool = False, timeout: int = 180):
    body = {
        "inputs": {},
        "query": query,
        "response_mode": "streaming" if stream else "blocking",
        "user": user,
    }
    req = urllib.request.Request(
        BASE,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not stream:
            data = json.loads(resp.read().decode("utf-8"))
            return data, time.time() - t0, 0.0
        # SSE：累计首个 token 延迟与总耗时
        answer = ""
        first_token = None
        total = 0.0
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                if payload == "[DONE]":
                    break
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "message":
                chunk = ev.get("answer") or ""
                if chunk and first_token is None:
                    first_token = time.time() - t0
                answer += chunk
            total = time.time() - t0
        return {"answer": answer}, total, first_token or 0.0


def show_resources(data):
    """打印 retriever_resources —— Java 侧 FR-06 组装 Citation 的数据源。"""
    meta = data.get("metadata") or {}
    res = meta.get("retriever_resources") or []
    if not res:
        print("      retriever_resources: （空）")
        return
    print(f"      retriever_resources: {len(res)} 条")
    for i, r in enumerate(res, 1):
        # 实测 Dify 1.17 返回的是扁平结构：content/document_name 在顶层，
        # 不是旧的 {"segment": {...}, "document": {...}} 嵌套。
        seg = (r.get("content") or (r.get("segment") or {}).get("content") or "")
        doc = r.get("document_name") or (r.get("document") or {}).get("name") or ""
        print(f"        [{i}] score={r.get('score')}  "
              f"dataset={str(r.get('dataset_id', ''))[:8]}  "
              f"position={r.get('position')}  doc={str(doc)[:28]}")
        print(f"            {seg[:70].replace(chr(10), ' ')}…")


def read_key_from_db() -> str:
    """从 api_tokens 表按应用前缀读 Key，只进内存不落盘不打印。"""
    p = subprocess.run(
        ["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
         "-t", "-A", "-c",
         f"select token from api_tokens where id::text like '21e9da6b%' "
         f"and app_id='{APP_ID}';"],
        capture_output=True,
    )
    if p.returncode != 0:
        print("[x] 读库失败:", p.stderr.decode("utf-8", "replace")[:200])
        return ""
    return p.stdout.decode("utf-8").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="Dify 应用 API Key；不给则从 api_tokens 表按应用读取")
    ap.add_argument("--user", default="eval-user", help="user 标识（会话隔离用）")
    ap.add_argument("--stream", action="store_true", help="用 SSE 流式模式测（测首 token 延迟）")
    args = ap.parse_args()

    key = args.key or read_key_from_db()
    if not key:
        print("[x] 拿不到 API Key，先用 --key 传或确认 api_tokens 表里有该应用的 token")
        return 2
    print(f"[*] key 前缀 {key[:8]}***（不打印明文）")

    print(f"模式: {'SSE 流式' if args.stream else 'blocking'}\n")
    results = []

    for group, cases, expect in [
        ("应答类（期望答得出 + 命中关键数字）", ANSWER_CASES, "answer"),
        ("库外类（期望拒答）", REFUSE_CASES, "refuse"),
    ]:
        print("=" * 76)
        print(group)
        print("=" * 76)
        for query, keys in cases:
            try:
                data, total, first = call(key, query, args.user, args.stream)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200]
                print(f"[{e.code}] {query}\n      {body}\n")
                results.append((query, expect, False, "HTTP " + str(e.code)))
                continue
            except Exception as e:  # noqa: BLE001
                print(f"[ERR] {query}\n      {type(e).__name__}: {e}\n")
                results.append((query, expect, False, type(e).__name__))
                continue

            ans = data.get("answer") or ""
            hit = [k for k in keys if k in ans] if expect == "answer" else []
            refused = any(m in ans for m in REFUSE_MARKERS)
            ok = bool(hit) if expect == "answer" else refused

            latency = f"首token {first:.2f}s / 总 {total:.2f}s" if args.stream else f"总 {total:.2f}s"
            print(f"[{'PASS' if ok else 'FAIL'}] {query}")
            print(f"      {latency}")
            print(f"      答案: {ans[:150].replace(chr(10), ' ')}")
            if expect == "answer":
                print(f"      关键数字命中: {hit or '无'}")
            else:
                print(f"      拒答标志: {'有' if refused else '无'}")
            show_resources(data)
            print()
            results.append((query, expect, ok, ",".join(hit) if hit else ""))

    print("=" * 76)
    print("汇总")
    print("=" * 76)
    for kind in ("answer", "refuse"):
        sel = [r for r in results if r[1] == kind]
        if not sel:
            continue
        passed = sum(1 for r in sel if r[2])
        print(f"  {kind:<8} {passed}/{len(sel)} 通过")
    print()
    print("AC-04 参考：库外类需全部拒答；应答类误拒率需 ≤10%")
    return 0


if __name__ == "__main__":
    sys.exit(main())

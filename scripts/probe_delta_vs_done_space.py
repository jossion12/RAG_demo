# -*- coding: utf-8 -*-
"""决定性实验：同一个空格，走 delta(SSE) 和走 done(JSON) 是否都能存活。

背景（已测事实，逐条排除）：
  1. probe_sse_space_count.py：data: 后有 51 帧带 >=2 空格
     => 客户端剥掉分隔空格是**正确**的，内容空格没被剥
  2. diag_space_loss.py：delta 拼接比 done 定稿少 5 个字符，**全是空格**
  3. ChatService.java / DifyClient.java：delta 与 answer 同源、原样透传，
     没有 trim() / substring()
  ⇒ 只剩一个可能：**SseEmitter 序列化 delta 时**把内容首字符的空格当成了
    自己的分隔空格丢掉，而 JSON 路径不会。

验证方法：找一帧「内容以空格开头」的 delta（data: 后 >=2 个空格），
把它的内容与 done 定稿里对应的片段对起来数长度。
若 delta 侧系统性少 1 个空格、SSE 序列化是元凶，则结论成立。

注意：不能靠「数总长度」下结论——要逐帧定位到具体哪一帧少。

用法：
    python scripts/probe_delta_vs_done_space.py
"""
import io
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("RAG_BASE", "http://localhost:8080/api/chat/stream")


def fetch(q):
    req = urllib.request.Request(
        BASE, data=json.dumps({"question": q}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=300).read().decode("utf-8")


def parse(raw):
    """按 SSE 规范解析：data: 后若有空格，剥**一个**。

    delta 的 payload 现在是 JSON 字符串（服务端改用 sendDelta，见 ChatService 注释），
    必须 JSON.parse 才能还原原文。第一版没 parse，直接把 `"根据"` 的引号也拼进去，
    算出「delta 比 done 长 1445」这种荒谬结果——那是探针没跟上协议变更。
    """
    deltas, done = [], None
    for frame in raw.split("\n\n"):
        if not frame.strip():
            continue
        event = ""
        data = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                v = line[5:]
                if v.startswith(" "):
                    v = v[1:]
                data.append(v)
        if not data:
            continue
        payload = "\n".join(data)
        if event == "delta":
            try:
                v = json.loads(payload)
                deltas.append(v if isinstance(v, str) else payload)
            except Exception:
                deltas.append(payload)   # 后端未升级：裸文本
        elif event == "done":
            done = payload
    return deltas, done


def main():
    q = "哪些情况保险公司不赔？"
    raw = fetch(q)
    deltas, done_raw = parse(raw)

    # 前端实际收到的（前端也是剥一个空格，和 parse 一致）
    client_joined = "".join(deltas)
    done = json.loads(done_raw)
    final = done.get("answer", "")

    print("delta 帧数 %d" % len(deltas))
    print("客户端拼接长度 %d" % len(client_joined))
    print("done.answer 长度 %d" % len(final))
    print("差 %d 字符" % (len(final) - len(client_joined)))
    print()

    # raw delta 帧（含 JSON 引号），统计 data: 后紧跟空格的数量
    raw_deltas = []
    for frame in raw.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data:"):
                raw_deltas.append(line[5:])
    n_strip = sum(1 for d in raw_deltas if d.startswith(" "))
    print("raw delta 帧 %d，其中 data: 后紧跟分隔空格的有 %d 帧" % (len(raw_deltas), n_strip))
    print("（delta 现在是 JSON 字符串，payload 不可能以裸空格开头，故应为 0）")
    print()

    # 找第一处差异
    i = 0
    while i < min(len(client_joined), len(final)) and client_joined[i] == final[i]:
        i += 1
    print("第一处差异在第 %d 字：" % i)
    print("  done : ...%s..." % final[max(0, i - 24):i + 24].replace("\n", "\\n"))
    print("  delta: ...%s..." % client_joined[max(0, i - 24):i + 24].replace("\n", "\\n"))
    print()

    print("=" * 68)
    if client_joined == final:
        print("✅ delta 拼接结果与 done.answer **逐字相同**——空格丢失已修复。")
    elif final.replace(" ", "") == client_joined.replace(" ", ""):
        print("⚠️ 仍有纯空格差异 %d 个。" % (len(final) - len(client_joined)))
    else:
        print("⚠️ 仍有非空格差异，需要继续查。")


if __name__ == "__main__":
    main()

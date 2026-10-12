# -*- coding: utf-8 -*-
"""端到端验证治理层：入参过滤 / 脱敏落库 / 拒答 / 降级。

这些断言对应需求里的 AC-04、AC-06，且**不依赖 Dify**——
Dify 未配好时也要能验治理层本身是对的。
"""
import io
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"

PASS, FAIL = [], []


def post(path, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=60):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  <- " + detail) if detail and not cond else ""))


def main():
    print("治理层端到端验证  base=%s\n" % BASE)

    print("AC-06 入参过滤")
    r = post("/api/chat", {"question": "这个产品能保证收益吗？"})
    check("合规红线被拦截", "[已拦截" in (r.get("answer") or ""), r.get("answer"))
    check("拦截时标记 refused", r.get("refused") is True)
    check("记录命中的红线类型", bool(r.get("blockedBy")), str(r.get("blockedBy")))

    r2 = post("/api/chat", {"question": "稳赚不赔吗？"})
    check("另一条红线同样拦截", "[已拦截" in (r2.get("answer") or ""), r2.get("answer"))

    print("\nAC-06 脱敏落库")
    ID = "110101199003078515"
    r3 = post("/api/chat", {"question": "我的身份证是%s，请问等待期是多久？" % ID})
    hist = get("/api/chat/history")
    blob = json.dumps(hist, ensure_ascii=False)
    check("历史中不含身份证明文", ID not in blob)
    hit = [c for c in hist if "110" in (c.get("question") or "")]
    check("落库的是掩码值", bool(hit), "未找到含掩码的记录")
    if hit:
        check("掩码值格式正确", "110***********8515" in hit[0]["question"],
              hit[0]["question"])
        check("记录了掩码标签", bool(hit[0].get("maskedLabels")), str(hit[0].get("maskedLabels")))

    print("\nAC-04 拒答 / 降级")
    r4 = post("/api/chat", {"question": "北京今天天气怎么样？"})
    check("无上游时也不抛 500", isinstance(r4, dict) and "answer" in r4, str(r4)[:100])
    check("返回明确的拒答/降级提示",
          r4.get("refused") is True or "未" in (r4.get("answer") or "") or "不可用" in (r4.get("answer") or ""),
          r4.get("answer"))
    check("无引用时不伪造 citations", not r4.get("citations"), str(r4.get("citations")))

    print("\n审计完整性")
    check("每条请求都留痕", len(hist) >= 3, "共 %d 条" % len(hist))
    check("留痕含拒答标记", any(c.get("refused") for c in hist))

    print("\n" + "=" * 56)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
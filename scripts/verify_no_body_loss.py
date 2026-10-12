# -*- coding: utf-8 -*-
"""核验裁页只删了目录，没删正文。

为什么必须查：strip_toc_pages.py 用「首个含『简称』的页」当正文起点。
判据一旦错一位，整份条款就会从中间开始——索引照样 completed、检索照样
返回结果，但评测分数会莫名其妙地全线下降，而且看不出是数据少了。
所以这里逐条比对裁剪前后，确认**丢掉的只有封面/目录/阅读指引**，
正文里的关键条款（等待期/犹豫期/免赔额/责任免除/宽限期）一条不少。

用法：
    python scripts/verify_no_body_loss.py
"""
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "source"
NOC = SRC / "noc"

# 评测集真正会问到的条款锚点。取自 eval/eval_cases_v2.json 的
# expected_clause 字段 —— 少任何一条，那类题就会从 coverage_gap 变成
# 「知识库里根本没有」，指标就废了。
ANCHORS = ["等待期", "犹豫期", "免赔额", "责任免除", "宽限期", "保险责任",
           "赔付比例", "费用补偿", "理赔", "诉讼时效", "受益人", "现金价值",
           "如实告知", "联系方式变更", "不可逆", "质子重离子"]


def text_of(p):
    from pypdf import PdfReader
    return re.sub(r"\s+", "", "\n".join(
        (pg.extract_text() or "") for pg in PdfReader(str(p)).pages))


def main():
    bad = 0
    for old in sorted(SRC.glob("*.pdf")):
        new = NOC / old.name
        if not new.exists():
            print("[!] %s 缺少裁剪版" % old.name)
            bad += 1
            continue
        a, b = text_of(old), text_of(new)
        # 裁剪版应当是原文的**后缀**（去掉开头若干页）
        suffix = b and b in a
        print("%s" % old.name)
        print("  原 %d 字 -> 裁 %d 字（去掉 %d 字，%.1f%%）"
              % (len(a), len(b), len(a) - len(b), 100.0 * (len(a) - len(b)) / max(len(a), 1)))
        print("  裁剪版是原文后缀: %s %s" % (suffix, "✅" if suffix else "❌ 正文有丢失或被改动"))
        if not suffix:
            bad += 1
        missing = [k for k in ANCHORS if k in a and k not in b]
        if missing:
            print("  ❌ 丢失关键条款: %s" % missing)
            bad += 1
        else:
            hit = sum(1 for k in ANCHORS if k in b)
            print("  关键条款锚点 %d/%d 全部保留 ✅" % (hit, len(ANCHORS)))
        # 目录页特征应当只存在于原文
        for marker in ("阅读指引", "条款目录"):
            if marker in b:
                print("  ❌ 裁剪版仍含「%s」" % marker)
                bad += 1
        print()

    print("=" * 60)
    print("结论: %s" % ("✅ 只丢了封面/目录，正文完整" if bad == 0
                        else "❌ 有 %d 处异常，需人工确认" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

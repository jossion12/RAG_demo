# -*- coding: utf-8 -*-
"""裁掉保险条款 PDF 的封面/阅读指引/条款目录页，生成干净 PDF 供重灌知识库。

为什么需要它（实测依据）：
  eval/runs/r004_prompt/report.md 记录「⚠ 目录页噪声 18/52 = 35%」——
  前 3 条引用里出现「阅读指引/条款目录/释义」页。这些页对「犹豫期/等待期/
  责任免除」这类词面命中极高，会把正文条款挤出候选，是 coverage_gap 和
  clause_mismatch 的主要噪声源之一。调参（top_k/threshold）救不了：
  docs/数据源验证报告.md 已写明「必须区分目录条目与正文条款」。

怎么判断正文从哪一页开始（不写死页码）：
  条款 PDF 的结构是「页眉公司名 + 文档名」重复出现在每一页开头，
  目录页之后正文第一页会出现定义句「……简称……」（如
  「“太保团体终身重疾 E”简称“团终身重疾 E”」）。这行只会出现在正文，
  目录页不会有。所以取**首个含「简称」的页**作为正文起始页。

用法：
    python scripts/strip_toc_pages.py            # 只报告，不写文件
    python scripts/strip_toc_pages.py --apply    # 写入 data/source/noc/
"""
import argparse
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "source")
DST = os.path.join(SRC, "noc")

from pypdf import PdfReader, PdfWriter  # noqa: E402


def flat(page):
    return re.sub(r"\s+", "", page.extract_text() or "")


def body_start(reader, probe=6):
    """正文起始页（1-based）。首个含「简称」且不含目录标记的页。"""
    for i, page in enumerate(reader.pages, 1):
        if i > probe:
            break
        t = flat(page)
        if "简称" in t and "条款目录" not in t and "阅读指引" not in t:
            return i
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写出 PDF（默认只报告）")
    args = ap.parse_args()

    if args.apply:
        os.makedirs(DST, exist_ok=True)

    pdfs = sorted(f for f in os.listdir(SRC) if f.lower().endswith(".pdf"))
    for name in pdfs:
        src = os.path.join(SRC, name)
        reader = PdfReader(src)
        start = body_start(reader)
        kept = len(reader.pages) - (start - 1)
        head = flat(reader.pages[start - 1])[:60]
        print("%s\n  页数 %d -> 丢弃封面/目录 p1..p%d，保留 %d 页"
              % (name, len(reader.pages), start - 1, kept))
        print("  正文首行: %s" % head)
        if not args.apply:
            continue

        writer = PdfWriter()
        for page in reader.pages[start - 1:]:
            writer.add_page(page)
        writer.add_metadata({"/Title": os.path.splitext(name)[0]})
        dst = os.path.join(DST, os.path.splitext(name)[0] + ".pdf")
        with open(dst, "wb") as fh:
            writer.write(fh)
        print("  已写出 %s (%d bytes)" % (dst, os.path.getsize(dst)))
        print()

    if not args.apply:
        print("以上为 dry-run，加 --apply 执行裁剪。")


if __name__ == "__main__":
    main()

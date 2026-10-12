# -*- coding: utf-8 -*-
"""生成 RAG_demo 项目流程图（自包含 HTML + Mermaid Markdown），输出到 docs/。

用法：
    python scripts/gen_flowcharts.py
产物：
    docs/项目流程图.html   内联 SVG，无外部依赖，浏览器直接打开
    docs/项目流程图.md     Mermaid 版，供 GitHub / IDE 预览与编辑
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_HTML = os.path.join(ROOT, "docs", "项目流程图.html")
OUT_MD = os.path.join(ROOT, "docs", "项目流程图.md")

# ---------- 样式 ----------
TYPES = {
    "frontend": ("#fde7f3", "#c2185b"),
    "primary":  ("#e3f0ff", "#1565c0"),
    "dify":     ("#e6f4ea", "#2e7d32"),
    "external": ("#eef2f7", "#5f6368"),
    "decision": ("#fff3cd", "#b8860b"),
    "danger":   ("#fde0e0", "#c62828"),
    "success":  ("#e0f2e9", "#2e8b57"),
}
DARK = "#1f2937"
ARROW = "#6b7280"


class Node:
    def __init__(self, id, x, y, w, t, lines=None, type="primary", dashed=False, h=None):
        self.id = id
        self.x = x
        self.y = y
        self.w = w
        self.t = t
        self.lines = lines or []
        self.type = type
        self.dashed = dashed
        body = len(self.lines)
        self.h = h if h else (34 + (16 if body else 0) + body * 16 + 14)


class Edge:
    def __init__(self, f, t, label="", fs="b", ts="t", elbow=False, dashed=False, curve=False):
        self.f = f
        self.t = t
        self.label = label
        self.fs = fs
        self.ts = ts
        self.elbow = elbow
        self.dashed = dashed
        self.curve = curve


class Diagram:
    def __init__(self, title, nodes, edges, w, h, note=None):
        self.title = title
        self.nodes = {n.id: n for n in nodes}
        self.edges = edges
        self.w = w
        self.h = h
        self.note = note


def anchor(n, side):
    if side == "t":
        return n.x + n.w / 2, n.y
    if side == "b":
        return n.x + n.w / 2, n.y + n.h
    if side == "l":
        return n.x, n.y + n.h / 2
    if side == "r":
        return n.x + n.w, n.y + n.h / 2
    raise ValueError(side)


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def node_svg(n):
    fill, stroke = TYPES[n.type]
    dash = ' stroke-dasharray="6 4"' if n.dashed else ""
    rect = (f'<rect x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" rx="9" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"{dash}/>')
    ty = n.y + 22
    title = (f'<text x="{n.x + n.w/2}" y="{ty}" text-anchor="middle" '
             f'font-size="14" font-weight="700" fill="{DARK}">{esc(n.t)}</text>')
    parts = [rect, title]
    cy = ty + 18
    for ln in n.lines:
        parts.append(f'<text x="{n.x + n.w/2}" y="{cy}" text-anchor="middle" '
                     f'font-size="11" fill="#374151">{esc(ln)}</text>')
        cy += 16
    return "\n".join(parts)


def edge_path(x1, y1, x2, y2, e):
    if e.elbow:
        mx = (x1 + x2) / 2
        d = f"M{x1:.1f} {y1:.1f} H{mx:.1f} V{y2:.1f} H{x2:.1f}"
    elif e.curve:
        # 简单弯曲：用二次贝塞尔
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        d = f"M{x1:.1f} {y1:.1f} Q{cx:.1f} {y1:.1f} {x2:.1f} {y2:.1f}"
    else:
        d = f"M{x1:.1f} {y1:.1f} L{x2:.1f} {y2:.1f}"
    return d


def edge_svg(e, nodes):
    f, t = nodes[e.f], nodes[e.t]
    x1, y1 = anchor(f, e.fs)
    x2, y2 = anchor(t, e.ts)
    d = edge_path(x1, y1, x2, y2, e)
    cls = ' class="el"' if e.dashed else ""
    stroke = 'stroke="#c62828" stroke-dasharray="6 4"' if e.dashed else f'stroke="{ARROW}"'
    path = (f'<path d="{d}" fill="none" {stroke} stroke-width="1.6" '
            f'marker-end="url(#arr)"{cls}/>')
    parts = [path]
    if e.label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if e.elbow:
            mx = (x1 + x2) / 2
            my = y1 if abs(y1 - y2) > 2 else (y1 + y2) / 2
        w = len(e.label) * 7.2 + 8
        parts.append(f'<rect x="{mx-w/2:.1f}" y="{my-9:.1f}" width="{w:.1f}" height="18" '
                     f'rx="4" fill="#ffffff" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{mx:.1f}" y="{my+4:.1f}" text-anchor="middle" '
                     f'font-size="11" fill="#374151">{esc(e.label)}</text>')
    return "\n".join(parts)


def render_svg(d):
    parts = [f'<svg viewBox="0 0 {d.w} {d.h}" width="100%" '
             f'xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,Segoe UI,'
             f'Roboto,Helvetica,Arial,sans-serif">']
    parts.append('<defs><marker id="arr" markerWidth="9" markerHeight="9" '
                 'refX="7" refY="3" orient="auto" markerUnits="strokeWidth">'
                 '<path d="M0,0 L7,3 L0,6 Z" fill="#6b7280"/></marker></defs>')
    for e in d.edges:
        parts.append(edge_svg(e, d.nodes))
    for n in d.nodes.values():
        parts.append(node_svg(n))
    if d.note:
        ny = d.h - 30
        parts.append(f'<text x="16" y="{ny}" font-size="12" fill="#6b7280">'
                     f'※ {esc(d.note)}</text>')
    parts.append('</svg>')
    return "\n".join(parts)


# =====================================================================
# 图 1：整体架构图
# =====================================================================
d1_nodes = [
    Node("fe", 20, 70, 200, "前端 React + Vite", ["localhost:5173", "fetch+TextDecoder 收 SSE"], "frontend"),
    Node("be", 280, 20, 260, "Java 治理层 (Spring Boot :8080)", [
        "ChatController · ChatService", "SensitiveDataFilter", "CitationAssembler", "ConversationRepository (JPA)"], "primary", h=170),
    Node("db", 280, 230, 260, "审计落库 H2 / PostgreSQL", ["Conversation 表"], "external"),
    Node("dify", 620, 80, 300, "Dify Chatflow 平台 (v1.17.1)", ["应用类型：advanced-chat", "端点 /v1/chat-messages"], "dify"),
    Node("ollama", 620, 215, 300, "Ollama 模型服务", ["LLM qwen3.5:9b", "Embedding bge-m3:latest"], "external"),
    Node("weav", 620, 335, 145, "Weaviate 向量库", ["3902 条向量"], "external"),
    Node("tei", 775, 335, 145, "TEI Rerank", ["bge-reranker-v2-m3"], "external"),
]
d1_edges = [
    Edge("fe", "be", "POST /api/chat/stream (SSE)", "r", "l"),
    Edge("be", "db", "审计落库 JPA", "b", "t"),
    Edge("be", "dify", "POST /v1/chat-messages\nBearer app-xxx", "r", "l"),
    Edge("dify", "ollama", "LLM 调用", "b", "t"),
    Edge("dify", "weav", "向量检索", "b", "t", curve=True),
    Edge("dify", "tei", "Rerank", "b", "t", curve=True),
    Edge("ollama", "weav", "索引 Embedding bge-m3", "b", "t", dashed=True),
]
d1 = Diagram("整体架构图", d1_nodes, d1_edges, 960, 420,
             note="三层：前端 → Java 治理层 → Dify 平台；Ollama/Weaviate/TEI 为 Dify 依赖的模型与向量服务。")

# =====================================================================
# 图 2：问答主链路（端到端 SSE）
# =====================================================================
d2_nodes = [
    Node("n1", 300, 20, 340, "前端：POST /api/chat/stream", ["fetch + ReadableStream", "TextDecoder 流式解析 SSE"], "frontend"),
    Node("n2", 300, 110, 340, "ChatController.stream()", ["新建 SseEmitter(180s)"], "primary"),
    Node("n3", 300, 195, 340, "① 入参过滤 filterInput()", ["正则：证件号 / 收益承诺语", "转发前必须先过滤"], "decision"),
    Node("d1", 380, 300, 180, "命中敏感？", [], "decision", h=54),
    Node("b1", 720, 288, 250, "拦截：send \"blocked\"", ["合规红线提示 → audit → return"], "danger"),
    Node("n4", 300, 390, 340, "② dify.streamChat().subscribe()", ["POST /v1/chat-messages (streaming)", "Bearer app-xxx"], "dify"),
    Node("b2", 720, 388, 250, "Dify 异常/超时", ["send \"error\" → emitter.complete()"], "danger"),
    Node("n5", 300, 480, 340, "收到 Delta → answer.append", ["send \"delta\"（正文增量）"], "primary"),
    Node("n6", 300, 565, 340, "收到 Finished → finish()", [], "primary"),
    Node("n7", 300, 645, 340, "③ 出参过滤 filterOutput()", ["累积完成后判定承诺语"], "decision"),
    Node("n8", 300, 730, 340, "④ CitationAssembler.assemble()", ["metadata.retriever_resources", "按 segmentId 去重"], "primary"),
    Node("n9", 300, 815, 340, "send \"citations\" + \"done\"", ["answer / refused / elapsed", "conversationId"], "primary"),
    Node("n10", 300, 900, 340, "⑤ audit() → 落库", ["Conversation (JPA→H2/PG)", "落库失败不影响已收答案"], "success"),
    Node("n11", 300, 985, 340, "前端渲染", ["流式正文 + 引用溯源(可展开)", "拒答 / 拦截提示"], "frontend"),
]
d2_edges = [
    Edge("n1", "n2", "", "b", "t"),
    Edge("n2", "n3", "", "b", "t"),
    Edge("n3", "d1", "", "b", "t"),
    Edge("d1", "b1", "是", "r", "l"),
    Edge("d1", "n4", "否", "b", "t"),
    Edge("n4", "b2", "异常/超时", "r", "l", dashed=True),
    Edge("n4", "n5", "", "b", "t"),
    Edge("n5", "n6", "", "b", "t"),
    Edge("n6", "n7", "", "b", "t"),
    Edge("n7", "n8", "", "b", "t"),
    Edge("n8", "n9", "", "b", "t"),
    Edge("n9", "n10", "", "b", "t"),
    Edge("n10", "n11", "", "b", "t"),
]
d2 = Diagram("问答主链路（端到端 SSE）", d2_nodes, d2_edges, 1000, 1075,
             note="顺序刻意：入参过滤在转发前；出参过滤在累积完成后；Dify 故障也先落审计再降级。")

# =====================================================================
# 图 3：治理层四步编排
# =====================================================================
d3_nodes = [
    Node("s1", 20, 50, 210, "① 入参过滤", ["filterInput()", "须在转发前，防明文出网"], "decision", h=78),
    Node("s2", 270, 50, 210, "② 转发 Dify 流式", ["streamChat().subscribe()", "正文增量实时回吐"], "dify", h=78),
    Node("s3", 520, 50, 210, "③ 出参过滤", ["filterOutput()", "累积完成后判整句承诺语"], "decision", h=78),
    Node("s4", 770, 50, 190, "④ 组装+落库", ["assemble 引用", "audit() 审计留痕"], "success", h=78),
    Node("bk", 270, 180, 210, "入参命中 → 拦截并审计后直接返回", [], "danger", h=46),
    Node("dg", 520, 180, 210, "Dify 不可用 → 落审计 + 降级返回", [], "danger", h=46),
    Node("nt", 20, 270, 940, "关键约定", [
        "过滤必须在转发前；Dify 不可用时仍先落审计再降级（故障时恰恰最需要留痕）；"
        "出参过滤在累积完成后做，流式过程无法判定整句；不误伤（等待期30日 不当证件号）。"], "external", h=64),
]
d3_edges = [
    Edge("s1", "s2", "", "r", "l"),
    Edge("s2", "s3", "", "r", "l"),
    Edge("s3", "s4", "", "r", "l"),
    Edge("s1", "bk", "", "b", "t", dashed=True),
    Edge("s2", "dg", "", "b", "t", dashed=True),
]
d3 = Diagram("后端治理层四步编排 (ChatService)", d3_nodes, d3_edges, 980, 360,
             note="四步顺序不可调换：入参过滤(防出网) → 转发Dify(流式) → 出参过滤(整句) → 组装引用+落库(留痕)。")

# =====================================================================
# 图 4：Dify Chatflow 检索链路
# =====================================================================
d4_nodes = [
    Node("start", 20, 40, 140, "开始", ["sys.query"], "external"),
    Node("kr", 200, 40, 200, "知识检索", ["仅选「保险条款」库", "节点级重配检索参数"], "dify"),
    Node("llm", 450, 40, 210, "LLM", ["qwen3.5:9b", "记忆关闭(FR-05单轮)"], "dify"),
    Node("ans", 710, 40, 170, "回答", ["输出 retriever_resources", "→ Java 侧读取"], "dify"),
    # 检索内部子图
    Node("zone", 180, 150, 700, "检索节点内部", [], "external", dashed=True, h=430),
    Node("q", 210, 175, 170, "Query", ["开始.sys.query"], "external"),
    Node("vec", 210, 250, 220, "向量召回", ["Weaviate + bge-m3"], "external"),
    Node("bm", 480, 250, 220, "关键词召回", ["BM25 全文"], "external"),
    Node("mg", 350, 335, 210, "合并（混合检索）", ["语义 0.7 / 关键词 0.3"], "decision"),
    Node("rr", 300, 415, 320, "TEI Rerank", ["bge-reranker-v2-m3"], "external"),
    Node("th", 300, 500, 320, "阈值 0.1 过滤 + TopK = 30", ["retriever_resources 输出"], "decision"),
    Node("prule", 450, 250, 0, "", [], "external"),  # placeholder removed below
]
d4_nodes = [n for n in d4_nodes if n.id != "prule"]
d4_edges = [
    Edge("start", "kr", "", "r", "l"),
    Edge("kr", "llm", "retriever_resources→上下文", "r", "l"),
    Edge("llm", "ans", "", "r", "l"),
    Edge("kr", "q", "检索入参", "b", "t", dashed=True),
    Edge("q", "vec", "", "b", "t"),
    Edge("q", "bm", "", "b", "t"),
    Edge("vec", "mg", "", "b", "t"),
    Edge("bm", "mg", "", "b", "t"),
    Edge("mg", "rr", "", "b", "t"),
    Edge("rr", "th", "", "b", "t"),
    Edge("th", "kr", "召回片段回灌", "t", "b", dashed=True, curve=True),
]
d4 = Diagram("Dify Chatflow 检索链路", d4_nodes, d4_edges, 900, 600,
             note="⚠ 节点级检索参数整体替换知识库级（非叠加），留空=0.0=不过滤，兜底拒答失效。LLM 提示词须含「无信息→未找到相关信息」「不追问澄清」。")

# =====================================================================
# 图 5：Dify 工作流搭建流程（6 步）
# =====================================================================
steps = [
    ("s1", "Step1 装插件", ["Ollama×2 (LLM+Emb)", "TEI (Rerank)"]),
    ("s2", "Step2 建知识库", ["高质量索引(不可逆)", "上传 3 份 PDF(单库)"]),
    ("s3", "Step3 知识库级检索", ["混合 + 权重", "Rerank + 阈值 + TopK"]),
    ("s4", "Step4 编 Chatflow", ["删参数提取器", "节点级重配检索(真正生效)"]),
    ("s5", "Step5 发布 + Key", ["生成 API Key", "仅显示一次/走环境变量"]),
    ("s6", "Step6 端到端验证", ["4 应答类", "4 库外拒答类"]),
]
d5_nodes = []
d5_edges = []
x = 20
bw = 145
for i, (sid, t, ls) in enumerate(steps):
    d5_nodes.append(Node(sid, x, 30, bw, t, ls, "primary", h=92))
    if i > 0:
        d5_edges.append(Edge(steps[i-1][0], sid, "", "r", "l"))
    x += bw + 25
warn = Node("w", 20, 150, x - 45, "⚠ 最易翻车点", [
    "节点级检索参数整体替换知识库级（不是继承）；阈值默认「关闭」须手动启用；"
    "Base URL 用 host.docker.internal 而非 127.0.0.1；模板自带悬空 dataset_id 须删。",
    "UI 未硬刷新就保存会覆盖脚本修复 → 改 draft + 已发布两份图。"], "danger", h=84)
d5_nodes.append(warn)
d5 = Diagram("Dify 工作流搭建流程（Step 1→6）", d5_nodes, d5_edges, x, 250,
             note="详见 docs/Dify-RAG工作流搭建手册.md（含 10 条踩坑记录）。")

# =====================================================================
# 图 6：评测流程
# =====================================================================
d6_nodes = [
    Node("e1", 20, 90, 200, "eval_cases_v2.json", ["52 条：应答/拒答/跨产品/口语", "歧义/多意图/鲁棒/幻觉陷阱"], "external"),
    Node("e2", 260, 90, 200, "run_iteration.py", ["走前端同款 SSE 链路", "逐条落盘 → 可 --resume"], "primary"),
    Node("e3", 500, 90, 200, "POST /api/chat/stream", ["逐条问 → 收 answer/citations"], "dify"),
    Node("e4", 740, 90, 200, "失败归因", ["串档/同名章节错配", "误拒/无据之答/要点缺失"], "decision"),
    Node("e5", 480, 220, 460, "runs/<tag>/ + 验收线",
         ["应答≥85% (AC-02)", "拒答≥85% (AC-04)", "跨产品≥90% (AC-09)",
          "口语≥75% (AC-10)", "引用≥90% (AC-03)"], "success", h=104),
]
d6_edges = [
    Edge("e1", "e2", "", "r", "l"),
    Edge("e2", "e3", "", "r", "l"),
    Edge("e3", "e4", "", "r", "l"),
    Edge("e4", "e5", "", "b", "t", elbow=True),
]
d6 = Diagram("评测流程 (FR-09)", d6_nodes, d6_edges, 960, 350,
             note="判分口径：覆盖率=命中 expected_answer_points / 总数；拒答=含「未找到相关信息」等标记或引用为空；引用命中=任一 citation 含 expected_clause。")

# =====================================================================
# 图 7：敏感数据过滤流程
# =====================================================================
d7_nodes = [
    Node("i", 20, 40, 190, "用户输入问题", [], "frontend"),
    Node("fi", 260, 30, 250, "filterInput() 入参过滤", [
        "正则 (?<![0-9])(\\d{17}[0-9Xx])(?![0-9]) 匹配证件号",
        "收益承诺语命中 → blocked"], "decision", h=92),
    Node("yes1", 580, 20, 230, "命中 → 拦截（不转发）", ["send \"blocked\"", "audit → return"], "danger"),
    Node("no1", 580, 130, 230, "脱敏后 question", ["进入 Dify"], "primary"),
    Node("dy", 580, 220, 230, "Dify 生成 answer", ["检索 + LLM"], "dify"),
    Node("fo", 580, 310, 250, "filterOutput() 出参过滤", [
        "累积完成后检测承诺语",
        "整句判定（流式无法判）"], "decision", h=92),
    Node("yes2", 890, 310, 220, "出参命中 → 替换提示", ["[已拦截：输出含合规红线]"], "danger"),
    Node("ok", 890, 130, 220, "原样输出 answer", [], "success"),
    Node("nt", 20, 430, 1090, "不误伤", [
        "doesNotMaskShortNumbers：「等待期 30 日」不当证件号（用前后非数字断言，不截取更长数字串）。"], "external", h=56),
]
d7_edges = [
    Edge("i", "fi", "", "r", "l"),
    Edge("fi", "yes1", "是", "r", "l"),
    Edge("fi", "no1", "否", "r", "l"),
    Edge("no1", "dy", "", "b", "t"),
    Edge("dy", "fo", "", "b", "t"),
    Edge("fo", "yes2", "是", "r", "l"),
    Edge("fo", "ok", "否", "r", "l"),
]
d7 = Diagram("敏感数据过滤流程 (FR-08)", d7_nodes, d7_edges, 1130, 510,
             note="入参在转发前拦截；出参在累积完成后判定。密钥与身份证号同级敏感，日志不落敏感值。")

# =====================================================================
diagrams = [d2, d3, d1, d4, d5, d6, d7]

# ----------------- HTML -----------------
def build_html():
    css = """
    *{box-sizing:border-box}
    body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         background:#f7f8fa;color:#1f2937}
    header{background:#1565c0;color:#fff;padding:22px 28px}
    header h1{margin:0;font-size:22px}
    header p{margin:6px 0 0;opacity:.9;font-size:13px}
    main{padding:24px 28px 60px;max-width:1180px;margin:0 auto}
    section{background:#fff;border:1px solid #e5e7eb;border-radius:12px;
            padding:18px 20px;margin-bottom:26px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
    section h2{margin:0 0 4px;font-size:17px;color:#111827}
    section .sub{margin:0 0 14px;font-size:12px;color:#6b7280}
    svg{display:block;width:100%;height:auto;background:#fff}
    footer{padding:0 28px 40px;max-width:1180px;margin:0 auto;color:#6b7280;font-size:12px}
    """
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>保险条款智能问答助手 · 项目流程图</title><style>' + css + '</style></head><body>']
    parts.append('<header><h1>保险条款智能问答助手 — 项目流程图</h1>'
                 '<p>RAG（Dify 编排）· Java 治理层（BFF/审计/过滤/引用组装/评测）· React 前端</p></header>')
    parts.append('<main>')
    subs = {
        "问答主链路（端到端 SSE）": "一次问答从前端发起到落库审计的完整事件序列（含拦截/降级分支）",
        "后端治理层四步编排 (ChatService)": "ChatService.chat() 的刻意顺序与两条安全分支",
        "整体架构图": "前端 / Java 治理层 / Dify 平台 / 模型与向量库三层依赖",
        "Dify Chatflow 检索链路": "Chatflow 节点链 + 知识检索内部（混合检索→Rerank→阈值→TopK）",
        "Dify 工作流搭建流程（Step 1→6）": "从装插件到端到端验证的搭建顺序与最易翻车点",
        "评测流程 (FR-09)": "评测集 → run_iteration.py → 前端链路取数 → 失败归因 → 验收线",
        "敏感数据过滤流程 (FR-08)": "入参/出参两条过滤路径与不误伤约束",
    }
    for d in diagrams:
        parts.append(f'<section><h2>{esc(d.title)}</h2>'
                     f'<p class="sub">{esc(subs.get(d.title, ""))}</p>'
                     f'{render_svg(d)}</section>')
    parts.append('</main>')
    parts.append('<footer>由 scripts/gen_flowcharts.py 生成 · 内联 SVG 无外部依赖 · 数据来源 README / Dify 手册 / 源码</footer>')
    parts.append('</body></html>')
    return "\n".join(parts)


# ----------------- Mermaid MD -----------------
MERMAID = {
    "问答主链路（端到端 SSE）": """```mermaid
flowchart TD
  FE[前端: POST /api/chat/stream\\nfetch+TextDecoder 收 SSE] --> CT[ChatController.stream\\nSseEmitter 180s]
  CT --> F1[① 入参过滤 filterInput\\n正则: 证件号/承诺语]
  F1 --> D1{命中敏感?}
  D1 -- 是 --> BL[拦截: send blocked\\naudit 后 return]
  D1 -- 否 --> DY[② dify.streamChat.subscribe\\nPOST /v1/chat-messages]
  DY -. 异常/超时 .-> ER[Dify 异常\\nsend error]
  DY --> DL[Delta → answer.append\\nsend delta]
  DL --> FN[Finished → finish]
  FN --> F2[③ 出参过滤 filterOutput\\n累积后判承诺语]
  F2 --> AS[④ CitationAssembler.assemble\\nretriever_resources 去重]
  AS --> SD[send citations + done]
  SD --> AU[⑤ audit 落库\\nConversation JPA]
  AU --> RN[前端渲染\\n流式正文+引用溯源+拒答提示]
```""",
    "后端治理层四步编排 (ChatService)": """```mermaid
flowchart LR
  S1[① 入参过滤\\nfilterInput 转发前] --> S2[② 转发 Dify 流式\\n正文增量回吐]
  S2 --> S3[③ 出参过滤\\nfilterOutput 整句判定]
  S3 --> S4[④ 组装+落库\\nassemble + audit 留痕]
  S1 -. 命中 .-> BK[拦截并审计后直接返回]
  S2 -. 不可用 .-> DG[Dify 故障→落审计+降级]
```""",
    "整体架构图": """```mermaid
flowchart TB
  FE[前端 React+Vite :5173] --> BE[Java 治理层 Spring Boot :8080]
  BE --> DB[(审计落库 H2/PostgreSQL)]
  BE --> DIFY[Dify Chatflow 平台 v1.17.1]
  DIFY --> OLL[Ollama: qwen3.5:9b / bge-m3]
  DIFY --> WV[(Weaviate 向量库 3902条)]
  DIFY --> TEI[TEI Rerank bge-reranker-v2-m3]
  OLL -. 索引 Embedding .-> WV
```""",
    "Dify Chatflow 检索链路": """```mermaid
flowchart TB
  ST[开始 sys.query] --> KR[知识检索\\n仅选保险条款库]
  KR --> LM[LLM qwen3.5:9b\\n记忆关闭]
  LM --> AN[回答\\n输出 retriever_resources]
  KR --> Q[Query]
  Q --> VEC[向量召回 Weaviate+bge-m3]
  Q --> BM[关键词召回 BM25]
  VEC --> MG[合并 语义0.7/关键词0.3]
  BM --> MG
  MG --> RR[TEI Rerank]
  RR --> TH[节点级阈值0.1过滤 + TopK=30]
  TH -. 召回片段回灌 .-> KR
```""",
    "Dify 工作流搭建流程（Step 1→6）": """```mermaid
flowchart LR
  S1[Step1 装插件\\nOllama×2+TEI] --> S2[Step2 建知识库\\n高质量索引+3PDF]
  S2 --> S3[Step3 知识库级检索\\n混合/Rerank/阈值/TopK]
  S3 --> S4[Step4 编Chatflow\\n删参数提取器+节点级重配]
  S4 --> S5[Step5 发布+Key\\n仅显示一次]
  S5 --> S6[Step6 验证\\n4应答+4拒答]
```""",
    "评测流程 (FR-09)": """```mermaid
flowchart LR
  E1[eval_cases_v2.json\\n52条] --> E2[run_iteration.py\\n前端同款SSE]
  E2 --> E3[POST /api/chat/stream\\n逐条收 answer/citations]
  E3 --> E4[失败归因\\n串档/误拒/要点缺失]
  E4 --> E5[runs/&lt;tag&gt;/ + 验收线]
```""",
    "敏感数据过滤流程 (FR-08)": """```mermaid
flowchart TD
  I[用户输入问题] --> FI[filterInput 入参过滤\\n证件号/承诺语正则]
  FI -- 是 --> Y1[拦截 不转发\\nsend blocked+audit]
  FI -- 否 --> N1[脱敏后 question]
  N1 --> DY[Dify 生成 answer]
  DY --> FO[filterOutput 出参过滤\\n累积后判承诺语]
  FO -- 是 --> Y2[替换拦截提示]
  FO -- 否 --> OK[原样输出]
```""",
}


def build_md():
    parts = ["# 保险条款智能问答助手 — 项目流程图", "",
             "> 由 `scripts/gen_flowcharts.py` 生成。Mermaid 版与 `docs/项目流程图.html` 同步维护。",
             "> 数据来源：`README.md` / `docs/Dify-RAG工作流搭建手册.md` / 后端 Java 源码。", ""]
    for d in diagrams:
        parts.append(f"## {d.title}")
        parts.append(MERMAID.get(d.title, ""))
        parts.append("")
    return "\n".join(parts)


def main():
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(build_html())
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(build_md())
    print("written:", OUT_HTML)
    print("written:", OUT_MD)


if __name__ == "__main__":
    main()

"""在 Dify api 容器内运行：用 Dify 自己的 schema 校验草稿图。

由 export_wf.py 调用（宿主��� -> docker cp -> docker exec）。
宿主机侧用 PowerShell 的 `>` 重定向导出会被控制台编码破坏中文，
所以导出与校验都走 subprocess 的字节通道，不经过 PowerShell 文本管道。

校验目的不是「JSON 合法」，而是「Dify 能把它解析成正确的节点对象」，
特别是 multiple_retrieval_config.score_threshold 必须真的是 0.5，
而不是运行时被源码 `else 0.0` 兜掉。
"""

import json
import sys

sys.path.insert(0, "/app/api")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.workflow.nodes.knowledge_retrieval.entities import KnowledgeRetrievalNodeData  # noqa: E402
from graphon.nodes.answer.entities import AnswerNodeData  # noqa: E402
from graphon.nodes.llm.entities import LLMNodeData  # noqa: E402

NODE_CLASSES = {
    "knowledge-retrieval": KnowledgeRetrievalNodeData,
    "answer": AnswerNodeData,
    "llm": LLMNodeData,
}

with open("/tmp/wf_fixed.json", encoding="utf-8") as f:
    g = json.load(f)
print(f"[OK] 图已载入，JSON 解析通过（{len(g['nodes'])} 节点）\n")

# ── schema 校验 ──
ok = True
for n in g["nodes"]:
    d, t = n["data"], n["data"]["type"]
    cls = NODE_CLASSES.get(t)
    if cls is None:
        print(f"[SKIP] {t}")
        continue
    try:
        cls.model_validate(d)
        print(f"[PASS] schema 解析 {t}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] schema 解析 {t}: {e}")

print()
parsed = KnowledgeRetrievalNodeData.model_validate(
    next(n["data"] for n in g["nodes"] if n["data"]["type"] == "knowledge-retrieval")
)
cfg = parsed.multiple_retrieval_config
llm = next(n["data"] for n in g["nodes"] if n["data"]["type"] == "llm")
answer = next(n["data"] for n in g["nodes"] if n["data"]["type"] == "answer")

print("=" * 70)
print("关键断言")
print("=" * 70)

checks = [
    ("dataset_ids 只剩「保险条款」一个",
     parsed.dataset_ids == ["b840bb7a-6289-4b5e-a09a-55e61e74043c"], parsed.dataset_ids),
    ("score_threshold 不为 None（None 会被源码兜成 0.0）",
     cfg.score_threshold is not None, cfg.score_threshold),
    ("score_threshold == 0.5", cfg.score_threshold == 0.5, cfg.score_threshold),
    ("top_k == 8", cfg.top_k == 8, cfg.top_k),
    ("reranking_enable is True", cfg.reranking_enable is True, cfg.reranking_enable),
    ("reranking_model 已指定且是 bge-reranker-v2-m3",
     cfg.reranking_model is not None and cfg.reranking_model.model == "bge-reranker-v2-m3",
     f"{cfg.reranking_model.provider}/{cfg.reranking_model.model}" if cfg.reranking_model else None),
    ("记忆变量关闭（单轮 FR-05）",
     llm.get("memory", {}).get("window", {}).get("enabled") is False,
     llm.get("memory", {}).get("window", {}).get("enabled")),
    ("LLM 上下文已开启", llm.get("context", {}).get("enabled") is True, None),
    ("提示词含拒答约束「未找到相关信息」",
     "未找到相关信息" in llm["prompt_template"][0]["text"], None),
    ("提示词不含「寻求澄清」（与 FR-05 冲突）",
     "寻求澄清" not in llm["prompt_template"][0]["text"], None),
    ("回答节点直连 LLM（无参数提取器）",
     str(llm and answer["answer"]).startswith("{{#") and "text#" in answer["answer"],
     answer["answer"]),
    ("parameter-extractor 节点已删除",
     not any(n["data"]["type"] == "parameter-extractor" for n in g["nodes"]), None),
]

for label, passed, actual in checks:
    print(f"[{'PASS' if passed else 'FAIL'}] {label}" + (f"  ->  {actual}" if actual is not None else ""))
    if not passed:
        ok = False

print("\n拓扑: " + " -> ".join(e["data"]["sourceType"] for e in g["edges"])
      + " -> " + g["edges"][-1]["data"]["targetType"])
print(f"节点 {len(g['nodes'])} 个，连线 {len(g['edges'])} 条")
print("\n" + ("=" * 70) + "\n" + ("ALL PASS" if ok else "HAS FAILURES"))
sys.exit(0 if ok else 1)

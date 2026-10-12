"""校验 draft 与已发布版本两份图是否都修好（容器内运行）。

由 export_all_graphs.py 调用：宿主机 -> docker cp -> docker exec。
宿主机用 PowerShell 的 `>` 重定向导出会被控制台编码破坏中文，
所以导出走 subprocess 字节通道。

用 Dify 自己的 KnowledgeRetrievalNodeData 解析——
score_threshold 只有被解析成 0.5 才算数，写成 0.5 但运行时被源码
`else 0.0` 兜掉的情况，只有这样才验得出来。
"""

import json
import sys

sys.path.insert(0, "/app/api")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.workflow.nodes.knowledge_retrieval.entities import KnowledgeRetrievalNodeData  # noqa: E402

DANGLING = {"6084ed3f-d100-4df2-a277-b40d639ea7c6", "b0de9dad-f9ad-46fe-a0fe-8e282b6731cb"}
GOOD_DATASET = "b840bb7a-6289-4b5e-a09a-55e61e74043c"

with open("/tmp/all_graphs.json", encoding="utf-8") as f:
    payload = json.load(f)

all_ok = True

for ver, graph_json in payload["rows"]:
    label = "draft" if ver == "draft" else f"published v{ver[:19]}"
    g = json.loads(graph_json)
    kr_raw = next((n["data"] for n in g["nodes"] if n["data"]["type"] == "knowledge-retrieval"), None)
    llm = next((n["data"] for n in g["nodes"] if n["data"]["type"] == "llm"), None)
    answer = next((n["data"] for n in g["nodes"] if n["data"]["type"] == "answer"), None)
    has_pe = any(n["data"]["type"] == "parameter-extractor" for n in g["nodes"])

    cfg = KnowledgeRetrievalNodeData.model_validate(kr_raw).multiple_retrieval_config if kr_raw else None

    checks = [
        ("无悬空知识库 ID", not (DANGLING & set(kr_raw.get("dataset_ids") or [])), kr_raw.get("dataset_ids")),
        ("只绑 1 个知识库", kr_raw.get("dataset_ids") == [GOOD_DATASET], None),
        ("score_threshold 解析为 0.3", cfg and cfg.score_threshold == 0.3, cfg.score_threshold if cfg else None),
        ("top_k = 8", cfg and cfg.top_k == 8, cfg.top_k if cfg else None),
        ("rerank 模型 = bge-reranker-v2-m3",
         cfg and cfg.reranking_model and cfg.reranking_model.model == "bge-reranker-v2-m3", None),
        ("参数提取器已删除", not has_pe, None),
        ("提示词含拒答约束", "未找到相关信息" in llm["prompt_template"][0]["text"], None),
        ("提示词无「寻求澄清」", "寻求澄清" not in llm["prompt_template"][0]["text"], None),
        ("记忆变量关闭", llm.get("memory", {}).get("window", {}).get("enabled") is False, None),
        ("思维链已关闭 think=False",
         (llm.get("model") or {}).get("completion_params", {}).get("think") is False,
         (llm.get("model") or {}).get("completion_params")),
        ("context 选择器已启用",
         bool(llm.get("context", {}).get("enabled")) and bool(llm.get("context", {}).get("variable_selector")),
         llm.get("context")),
        ("回答节点直连 LLM", "text#" in (answer.get("answer") or ""), answer.get("answer")),
        ("节点数 = 4", len(g["nodes"]) == 4, f"{len(g['nodes'])} 节点/{len(g['edges'])} 连线"),
    ]

    print("=" * 72)
    print(label)
    print("=" * 72)
    for c in checks:
        passed, actual = c[1], (c[2] if len(c) > 2 else None)
        print(f"  [{'PASS' if passed else 'FAIL'}] {c[0]}" + (f"  ->  {actual}" if actual is not None else ""))
        if not passed:
            all_ok = False
    print()

print("=" * 72)
print("结论: " + ("draft 与已发布版本均已修复" if all_ok else "仍有未修复项"))

"""修复 Dify Chatflow 草稿的图配置。

背景（全部由本机实测 + Dify 1.17 源码确认）
--------------------------------------------
1. knowledge-retrieval 节点绑了 3 个 dataset_id，其中 2 个在 datasets 表里不存在
   （模板「知识库+聊天机器人」自带的示例 ID）。

2. 节点级 multiple_retrieval_config 会【整体替换】知识库级设置，而不是叠加。
   源码 core/workflow/nodes/knowledge_retrieval/knowledge_retrieval_node.py:279：
       score_threshold = cfg.score_threshold if cfg.score_threshold is not None else 0.0
   原值 None → 变成 0.0 → 不做任何过滤 → 兜底拒答（FR-05）失效。

3. 模板自带「参数提取器」节点，参数描述是 "Dify的安装"（模板残留），
   会多跑一次 LLM，冲击 §4.1 的 3 秒预算。

4. LLM 提示词是模板默认的，含「就寻求澄清」，与 FR-05「单轮、不追问」冲突。

用法：
    python scripts/fix_workflow_graph.py            # 只预览 diff，不写库
    python scripts/fix_workflow_graph.py --apply    # 执行修复
"""

import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
GRAPH_FILE = ROOT / "_wf.json"
DB_CONTAINER = "docker-db_postgres-1"
APP_ID = "f5939e3a-5fd7-43e7-8824-54d6ed1bc8da"
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"  # 保险条款
DANGLING = {"6084ed3f-d100-4df2-a277-b40d639ea7c6", "b0de9dad-f9ad-46fe-a0fe-8e282b6731cb"}

RERANK_PROVIDER = "langgenius/huggingface_tei/huggingface_tei"
RERANK_MODEL = "bge-reranker-v2-m3"

NEW_PROMPT = """你是一名保险条款问答助手。请严格依据【检索到的条款内容】回答用户问题。

规则：
1. 只使用【检索到的条款内容】中的信息，不得用你自己的知识补充或推测。
2. 如果【检索到的条款内容】中没有回答问题所需的信息，请直接回复"未找到相关信息"，不要猜测、不要编造。
3. 引用条款时保留原文的数字、日期、金额、天数等关键信息，不要改写数值。
4. 答案末尾不要添加免责提示或任何额外说明。
5. 这是单轮问答，不需要追问或澄清。

【检索到的条款内容】
{{#context#}}

【用户问题】
{{#sys.query#}}"""


def psql(sql: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", "postgres", "-d", "dify", "-t", "-A", "-c", sql],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 失败: {proc.stderr.strip()}")
    return proc.stdout.strip()


def main() -> int:
    if not GRAPH_FILE.exists():
        print(f"[x] 找不到 {GRAPH_FILE}，请先导出 graph JSON")
        return 2

    g = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))

    # ── 备份 ────────────────────────────────────────────
    bak = ROOT / f"_wf_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    shutil.copy2(GRAPH_FILE, bak)
    print(f"[*] 已备份原图 -> {bak.name}\n")

    changes = []
    llm_id = None
    answer_id = None
    drop_ids = set()

    for n in g["nodes"]:
        d = n["data"]
        t = d.get("type")

        if t == "knowledge-retrieval":
            old_ids = list(d.get("dataset_ids") or [])
            if DANGLING & set(old_ids):
                d["dataset_ids"] = [DATASET_ID]
                changes.append(f"知识检索.dataset_ids: {len(old_ids)} 个 -> 1 个（移除悬空 ID {sorted(DANGLING & set(old_ids))}）")
            elif old_ids != [DATASET_ID]:
                d["dataset_ids"] = [DATASET_ID]
                changes.append(f"知识检索.dataset_ids: {old_ids} -> [{DATASET_ID}]")

            before = dict(d.get("multiple_retrieval_config") or {})
            d["multiple_retrieval_config"] = {
                "top_k": 8,
                "score_threshold": 0.5,
                "reranking_mode": "reranking_model",
                "reranking_enable": True,
                "reranking_model": {"provider": RERANK_PROVIDER, "model": RERANK_MODEL},
            }
            changes.append("知识检索.multiple_retrieval_config:")
            for k in ("top_k", "score_threshold", "reranking_enable", "reranking_model", "reranking_mode"):
                changes.append(f"    {k}: {before.get(k)!r} -> {d['multiple_retrieval_config'][k]!r}")

        elif t == "llm":
            llm_id = n["id"]
            old_text = (d.get("prompt_template") or [{}])[0].get("text", "")
            if old_text != NEW_PROMPT:
                d["prompt_template"] = [{"role": "system", "text": NEW_PROMPT}]
                changes.append("LLM.prompt_template: 模板默认 -> 保险条款专用提示词")
            mem = d.get("memory") or {}
            if mem.get("window", {}).get("enabled") is not False:
                mem.setdefault("window", {})["enabled"] = False
                d["memory"] = mem
                changes.append("LLM.memory.window.enabled -> false（单轮，FR-05）")
            else:
                changes.append("LLM.memory.window.enabled 已是 false，保持（单轮符合 FR-05）")

        elif t == "answer":
            answer_id = n["id"]

        elif t == "parameter-extractor":
            drop_ids.add(n["id"])
            changes.append(f"删除节点 parameter-extractor ({n['id']}) —— 模板残留，多一次 LLM 调用")

    if not drop_ids and len(g["nodes"]) == 4:
        changes.append("参数提取器节点已不存在，跳过")

    # ── 重建连线：LLM 直连回答 ──────────────────────────
    if llm_id and answer_id:
        new_edges = [
            e for e in g["edges"]
            if e["source"] not in drop_ids and e["target"] not in drop_ids
        ]
        if not any(e["source"] == llm_id and e["target"] == answer_id for e in new_edges):
            new_edges.append({
                "zIndex": 0,
                "sourceHandle": "source",
                "id": f"{llm_id}-source-{answer_id}-target",
                "target": answer_id,
                "data": {
                    "isInIteration": False,
                    "sourceType": "llm",
                    "targetType": "answer",
                    "isInLoop": False,
                    "loop_id": None,
                    "iteration_id": None,
                },
                "targetHandle": "target",
                "source": llm_id,
                "type": "custom",
            })
            changes.append(f"新增连线 LLM({llm_id}) -> 回答({answer_id})")

        removed = [e["id"] for e in g["edges"] if e not in new_edges]
        if removed:
            changes.append(f"移除连线: {removed}")
        g["edges"] = new_edges

    if drop_ids:
        g["nodes"] = [n for n in g["nodes"] if n["id"] not in drop_ids]

    # ── 回答节点输出改指 LLM ────────────────────────────
    for n in g["nodes"]:
        if n["data"].get("type") == "answer" and llm_id:
            old = n["data"].get("answer")
            n["data"]["answer"] = f"{{{{#{llm_id}.text#}}}}"
            if old != n["data"]["answer"]:
                changes.append(f"回答.answer: {old!r} -> {n['data']['answer']!r}")

    print("=" * 74)
    print("变更清单")
    print("=" * 74)
    for c in changes:
        print("  " + c)

    print("\n" + "=" * 74)
    print("修复后拓扑")
    print("=" * 74)
    for e in g["edges"]:
        st = e["data"].get("sourceType")
        tt = e["data"].get("targetType")
        print(f"  {st}  ->  {tt}")

    cfg = next((n["data"]["multiple_retrieval_config"] for n in g["nodes"]
                if n["data"].get("type") == "knowledge-retrieval"), None)
    print("\n修复后节点级检索配置:")
    print("  " + json.dumps(cfg, ensure_ascii=False))

    if "--apply" not in sys.argv:
        print("\n[*] 预览模式，未写库。确认无误后加 --apply 执行。")
        return 0

    # ── 写库：base64 传输，避免 JSON 里的引号破坏 SQL 字面量 ──
    payload = base64.b64encode(json.dumps(g, ensure_ascii=False).encode("utf-8")).decode()
    sql = (
        f"update workflows set graph = convert_from(decode('{payload}','base64'),'UTF8')::jsonb, "
        f"updated_at = now() where version='draft' and app_id='{APP_ID}' returning app_id;"
    )
    res = psql(sql)
    if not res:
        print("\n[x] 更新未命中任何行（草稿可能已被 UI 保存覆盖）")
        return 2
    print(f"\n[OK] 已写库: {res}")

    GRAPH_FILE.write_text(json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[*] 本地副本已同步: {GRAPH_FILE.name}")
    print("[*] 回到 Dify 页面请【硬刷新】（Ctrl+Shift+R）再改动，避免你的旧页面状态覆盖修复结果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

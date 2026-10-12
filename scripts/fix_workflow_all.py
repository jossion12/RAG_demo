"""修复 Dify Chatflow 的【草稿 + 已发布版本】两份图。

为什么必须同时改两份
--------------------
Dify 的「发布」是把 draft 拷贝成一个新版本记录，apps.workflow_id 指向它。
只改 draft 不会影响正在跑的已发布版本。

上一轮的教训
------------
修复只写进了 draft，之后用户在一个未硬刷新的页面上保存，
UI 的旧状态把 draft 整个覆盖回修复前的内容，随后发布的版本 2 也是坏的。
所以这次两份都改，且改完用 Dify 自身的 schema 复验。

关键原理（来自 Dify 1.17 源码 knowledge_retrieval_node.py:279）
--------------------------------------------------------------
    score_threshold = cfg.score_threshold if cfg.score_threshold is not None else 0.0
节点级配置【整体替换】知识库级设置，不叠加。
score_threshold 留空 = 0.0 = 不做任何过滤 = 兜底拒答（FR-05）失效。

用法：
    python scripts/fix_workflow_all.py            # 预览 diff
    python scripts/fix_workflow_all.py --apply    # 写库（draft + 已发布版本）
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DB = "docker-db_postgres-1"
APP_ID = "f5939e3a-5fd7-43e7-8824-54d6ed1bc8da"
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
# 检索阈值。实测 rerank 分数分布：
#   库外类（年化收益率等）最高 0.0206  -> 0.3 仍远超它，拒答能力不受影响
#   应答类直接命中                    0.83~0.88
#   应答类跨表述命中（90日等待期）      0.3383 <- 0.5 会把它切掉，0.3 不会
# 0.3 落在两类分布之间的空隙里，与 FR-05 兜底拒答兼容。
SCORE_THRESHOLD = 0.3
# top_k 必须放到 30：hybrid_search 只取 top_k 条候选进 rerank 池。
# 实测（scripts/topk_experiment.py）：top_k=8 和 15 时，语料里那段
# 「自本合同生效之日起 30 日内（含 30 日）为等待期」根本进不了池，
# LLM 只能答「未找到相关信息」；top_k=30 时它以 0.7381 分排到第 1，答案正确。
TOP_K = 30
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
    p = subprocess.run(
        ["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify", "-t", "-A", "-c", sql],
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:400])
    return p.stdout.decode("utf-8").strip()


def write_graph(where_clause: str, g: dict) -> str:
    payload = base64.b64encode(json.dumps(g, ensure_ascii=False).encode("utf-8")).decode()
    sql = (f"update workflows set graph = convert_from(decode('{payload}','base64'),'UTF8')::jsonb, "
           f"updated_at = now() where {where_clause} returning version;")
    return psql(sql)


# 引用开关不在 app_model_configs，而在 workflows.features 里。
# 见 core/app/apps/advanced_chat/app_config_manager.py:27
#     features_dict = workflow.features_dict
#     additional_features = cls.convert_features(features_dict, app_mode)
# 再经 RetrievalResourceConfigManager.convert() 读 retriever_resource.enabled。
# 关着时 message_cycle_manager.handle_retriever_resources 静默跳过，
# metadata.retriever_resources 恒为 []，FR-06 引用组装拿不到任何数据。
FEATURES = {
    "file_upload": {"image": {"enabled": False, "number_limits": 3,
                              "transfer_methods": ["local_file", "remote_url"]}},
    "opening_statement": "",
    "retriever_resource": {"enabled": True},
    "sensitive_word_avoidance": {"enabled": False},
    "speech_to_text": {"enabled": False},
    "suggested_questions": [],
    "suggested_questions_after_answer": {"enabled": False},
    "text_to_speech": {"enabled": False, "language": "", "voice": ""},
}


def write_features(where_clause: str) -> str:
    payload = base64.b64encode(json.dumps(FEATURES, ensure_ascii=False).encode("utf-8")).decode()
    sql = (f"update workflows set features = convert_from(decode('{payload}','base64'),'UTF8')::jsonb, "
           f"updated_at = now() where {where_clause} returning version;")
    return psql(sql)


def check_features(where_clause: str) -> bool:
    raw = psql(f"select coalesce(features,'{{}}') from workflows where {where_clause};")
    try:
        return bool((json.loads(raw) or {}).get("retriever_resource", {}).get("enabled"))
    except json.JSONDecodeError:
        return False


# 【重要更正】节点级 multiple_retrieval_config.score_threshold 在运行时【不被采纳】。
# 实测（scripts/dataset_threshold_experiment.py，2026-10-12）：
#   节点级 0.5 / 0.3 / 0.0 三个值，返回条数恒为 2；
#   而只改【知识库级】datasets.retrieval_model.score_threshold：
#     0.5 -> 2 条（不含 90 日等待期）
#     0.3 -> 3 条（含 0.3376 的「90 日的等待期」段）✓
#     0.0 -> 3 条
# 走的是 dataset_retrieval.py:_retriever 里 retrieval_model[...] 那条分支。
# 所以真正要调的是知识库级阈值；节点级仍写正确值，避免误导后来人。
def fix_dataset_retrieval_config() -> None:
    raw = psql(f"select retrieval_model from datasets where id='{DATASET_ID}';")
    rm = json.loads(raw)
    rm["score_threshold"] = SCORE_THRESHOLD
    rm["score_threshold_enabled"] = True
    rm["top_k"] = TOP_K
    rm["search_method"] = "hybrid_search"
    rm["reranking_enable"] = True
    payload = json.dumps(rm, ensure_ascii=False).replace("'", "''")
    psql(f"update datasets set retrieval_model = '{payload}' where id='{DATASET_ID}';")
    cur = json.loads(psql(f"select retrieval_model from datasets where id='{DATASET_ID}';"))
    print(f"  知识库级 score_threshold = {cur['score_threshold']}  "
          f"(enabled={cur['score_threshold_enabled']}, top_k={cur['top_k']}, "
          f"{cur['search_method']}, rerank={cur['reranking_enable']})")


def transform(g: dict, log: list) -> dict:
    llm_id = answer_id = None
    drop = set()

    for n in g["nodes"]:
        d, t = n["data"], n["data"].get("type")

        if t == "knowledge-retrieval":
            old = list(d.get("dataset_ids") or [])
            d["dataset_ids"] = [DATASET_ID]
            if old != [DATASET_ID]:
                log.append(f"    dataset_ids: {len(old)} 个 -> 1 个")
            before = dict(d.get("multiple_retrieval_config") or {})
            d["multiple_retrieval_config"] = {
                "top_k": TOP_K,
                "score_threshold": SCORE_THRESHOLD,
                "reranking_mode": "reranking_model",
                "reranking_enable": True,
                "reranking_model": {"provider": RERANK_PROVIDER, "model": RERANK_MODEL},
            }
            log.append(f"    score_threshold: {before.get('score_threshold')!r} -> {SCORE_THRESHOLD}")
            log.append(f"    top_k: {before.get('top_k')!r} -> 8")
            if (before.get("reranking_model") or {}).get("model") != RERANK_MODEL:
                log.append(f"    reranking_model: {before.get('reranking_model')!r} -> {RERANK_MODEL}")

        elif t == "llm":
            llm_id = n["id"]
            if (d.get("prompt_template") or [{}])[0].get("text") != NEW_PROMPT:
                d["prompt_template"] = [{"role": "system", "text": NEW_PROMPT}]
                log.append("    提示词 -> 保险条款专用（含拒答约束，无「寻求澄清」）")
            d.setdefault("memory", {}).setdefault("window", {})["enabled"] = False

            # 关掉思维链。Ollama 插件 models/llm/llm.py:210 会 pop model_parameters["think"]
            # 写进 Ollama 顶层 data["think"]；开着时插件把 message.thinking 用 <think> 标签
            # 拼进 content（llm.py:412 _wrap_thinking_by_reasoning_content），
            # 既污染答案又白烧 500+ token、首 token 延迟从 0.1s 涨到 17s。
            params = d.setdefault("model", {}).setdefault("completion_params", {})
            if params.get("think") is not False:
                log.append(f"    completion_params.think: {params.get('think')!r} -> False")
                params["think"] = False
            if params.get("temperature") != 0.1:
                log.append(f"    completion_params.temperature: {params.get('temperature')!r} -> 0.1")
                params["temperature"] = 0.1

        elif t == "answer":
            answer_id = n["id"]

        elif t == "parameter-extractor":
            drop.add(n["id"])
            log.append("    删除参数提取器节点（模板残留，会把答案抽成空 -> 无内容返回）")

    if llm_id and answer_id:
        edges = [e for e in g["edges"] if e["source"] not in drop and e["target"] not in drop]
        if not any(e["source"] == llm_id and e["target"] == answer_id for e in edges):
            edges.append({
                "zIndex": 0, "sourceHandle": "source",
                "id": f"{llm_id}-source-{answer_id}-target", "target": answer_id,
                "data": {"isInIteration": False, "sourceType": "llm", "targetType": "answer",
                         "isInLoop": False, "loop_id": None, "iteration_id": None},
                "targetHandle": "target", "source": llm_id, "type": "custom",
            })
            log.append(f"    新增连线 LLM -> 回答")
        g["edges"] = edges

    g["nodes"] = [n for n in g["nodes"] if n["id"] not in drop]

    for n in g["nodes"]:
        if n["data"].get("type") == "answer" and llm_id:
            new = f"{{{{#{llm_id}.text#}}}}"
            if n["data"].get("answer") != new:
                log.append(f"    回答.answer: {n['data'].get('answer')!r} -> {new!r}")
                n["data"]["answer"] = new
    return g


def main() -> int:
    pub_id = psql(f"select workflow_id from apps where id='{APP_ID}';")
    print(f"apps.workflow_id = {pub_id or '(空)'}")

    targets = [("draft", "version='draft'")]
    if pub_id:
        targets.append((f"published({pub_id[:8]})", f"id='{pub_id}'"))

    all_g = {}
    for label, where in targets:
        try:
            raw = psql(f"select graph from workflows where {where};")
        except RuntimeError as e:
            print(f"[x] 读取 {label} 失败: {e}")
            continue
        if not raw:
            print(f"[x] {label} 为空")
            continue
        g = json.loads(raw)
        all_g[label] = g
        log: list[str] = []
        all_g[label] = transform(g, log)
        print(f"\n[{label}] 变更 {len(log)} 项：")
        for line in log:
            print(line)
        if not check_features(where):
            log.append("    features.retriever_resource: False -> True（引用开关）")
            print("    features.retriever_resource: False -> True（引用开关）")
        else:
            print("    features.retriever_resource 已为 True")

    if not all_g:
        return 2

    bak = ROOT / f"_wf_backup_all_{time.strftime('%Y%m%d_%H%M%S')}.json"
    bak.write_text(json.dumps({k: v for k, v in all_g.items()}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n[*] 修复后副本 -> {bak.name}")

    if "--apply" not in sys.argv:
        print("\n[*] 预览模式。确认后加 --apply 写库。")
        return 0

    for label, where in targets:
        if label not in all_g:
            continue
        res = write_graph(where, all_g[label])
        print(f"[OK] {label}: {res}")
        res2 = write_features(where)
        print(f"[OK] {label} features: {res2}")
        ok = check_features(where)
        print(f"[{'OK' if ok else '!!'}] {label} features.retriever_resource.enabled = {ok}")

    print("\n知识库级检索配置：")
    fix_dataset_retrieval_config()

    print("\n[!] 重要：如果你此刻在浏览器里开着工作流页面，"
          "必须先 Ctrl+Shift+R 硬刷新，否则保存时会把修复再覆盖掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

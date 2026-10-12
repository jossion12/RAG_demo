"""隔离测试 _convert_to_original_retriever_resource / _build_array_context_event。

为什么不继续读源码：整条链路有 4 个环节（事件产出 -> graphon _collect ->
workflow_app_runner 分发 -> message_cycle_manager 落 metadata），
任何一环断了现象都是「retriever_resources 为空」，读代码无法定位。
这里只测最容易出错的第一环：解析逻辑本身吐不吐 retriever_resource。

用法：
    docker cp diag_retriever_parse.py docker-api-1:/tmp/
    docker exec -e PYTHONIOENCODING=utf-8 docker-api-1 python /tmp/diag_retriever_parse.py
"""

import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 从库里取一次真实的检索节点输出当样本，避免手写样本与真实结构不符
GET_SQL = ("select outputs from workflow_node_executions "
           "where node_type='knowledge-retrieval' order by created_at desc limit 1;")


def fetch_sample():
    # 样本由宿主机取好放在 /tmp/sample.json 再 docker cp 进来——
    # 容器里没有 docker CLI，没法自己连库。
    with open("/tmp/sample.json", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    sample = fetch_sample()
    if not sample:
        return 2
    items = sample.get("result") or []
    print(f"[*] 样本检索条数 = {len(items)}")
    if not items:
        print("[x] 样本为空，换一次运行再试")
        return 2

    it = items[0]
    print(f"[*] item[0] keys = {list(it.keys())}")
    print(f"[*] item[0].metadata._source = {(it.get('metadata') or {}).get('_source')!r}")
    print(f"[*] has 'content' = {'content' in it}")

    sys.path.insert(0, "/app/api")
    from graphon.nodes.llm.node import LLMNode  # noqa: E402
    from graphon.variables.variables import ArrayObjectVariable  # noqa: E402

    conv = LLMNode._convert_to_original_retriever_resource
    raw = conv(LLMNode, it)  # 未绑定实例，直接当普通函数调
    print(f"\n[1] _convert_to_original_retriever_resource(真实 item) ->")
    if raw is None:
        print("    None  <== 解析失败，资源被丢弃")
    else:
        print(f"    OK, keys={list(raw.keys())}")
        print(f"    document_name={raw.get('document_name')!r} score={raw.get('score')}")

    # 逐步二分：到底哪个条件不满足
    md = it.get("metadata") or {}
    print("\n[2] 逐条件检查")
    print(f"    'metadata' in item        = {'metadata' in it}")
    print(f"    '_source' in metadata     = {'_source' in md}")
    print(f"    _source == 'knowledge'    = {md.get('_source') == 'knowledge'}")

    print("\n[3] 走 _build_array_context_event")
    var = ArrayObjectVariable(value=items)
    print(f"    ArrayObjectVariable.isinstance(ArraySegment)?")
    from graphon.variables.segments import ArraySegment  # noqa: E402
    print(f"    -> {isinstance(var, ArraySegment)}")

    built = LLMNode._build_array_context_event(LLMNode, var)
    print(f"    context 长度 = {len(built.context)}")
    print(f"    retriever_resources 条数 = {len(built.retriever_resources)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
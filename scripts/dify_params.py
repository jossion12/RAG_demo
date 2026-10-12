# -*- coding: utf-8 -*-
"""Dify 检索/LLM 参数的读写与回滚。

为什么需要它：调参要能在「改配置 → 跑一轮 → 看结果 → 再改」之间往返，
而每一轮的结果必须能说清是在**哪组参数**下跑出来的。改之前自动落一份快照，
出问题一条命令回滚。

改的是 Dify 自己的 Postgres（复用 scripts/topk_experiment.py 已验证的做法），
不是改 Java 侧——Java 治理层不在本轮调参范围内。

生效位置（本机 Dify 1.17.1 实测）：
  - score_threshold / top_k / 权重 -> datasets.retrieval_model（**知识库级**才生效）
    节点级 multiple_retrieval_config 同名字段运行时不被采纳，调它只会得到「改了没反应」。
  - temperature / top_p / prompt  -> 已发布 workflow 里对应 LLM 节点
    （apps.workflow_id 指向的那份 workflows 行就是运行时用的已发布版本）

用法：
    python scripts/dify_params.py --show
    python scripts/dify_params.py --snapshot eval/params_snapshot.json
    python scripts/dify_params.py --set score_threshold=0.2 top_k=20
    python scripts/dify_params.py --restore eval/runs/r001_baseline/params_backup.json
"""
import argparse
import io
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "docker-db_postgres-1"
DATASET_ID = "b840bb7a-6289-4b5e-a09a-55e61e74043c"
APP_ID = "f5939e3a-5fd7-43e7-8824-54d6ed1bc8da"


def psql(sql):
    p = subprocess.run(["docker", "exec", DB, "psql", "-U", "postgres", "-d", "dify",
                        "-t", "-A", "-c", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:400])
    return p.stdout.decode("utf-8").strip()


def workflow_id():
    return psql("select workflow_id from apps where id='%s';" % APP_ID)


def read_state():
    ds = json.loads(psql("select retrieval_model from datasets where id='%s';" % DATASET_ID))
    wid = workflow_id()
    g = json.loads(psql("select graph from workflows where id='%s';" % wid))
    nodes = []
    for n in g["nodes"]:
        d = n["data"]
        if d.get("type") == "knowledge-retrieval":
            nodes.append({"node": n["id"], "title": d.get("title"),
                          "multiple_retrieval_config": d.get("multiple_retrieval_config")})
        elif d.get("type") == "llm":
            nodes.append({"node": n["id"], "title": d.get("title"),
                          "model": d.get("model", {}).get("name"),
                          "completion_params": d.get("model", {}).get("completion_params"),
                          # 存完整的 {role, text}：restore 要按 role 把整段换回去，
                          # 只存 text 的话回滚时无从判断哪条是 system。
                          "prompt_template": [{"role": t.get("role"), "text": t.get("text")}
                                              for t in d.get("prompt_template", [])]})
    return {"dataset_retrieval_model": ds, "workflow_id": wid, "nodes": nodes}


def write_dataset(rm):
    payload = json.dumps(rm, ensure_ascii=False).replace("'", "''")
    psql("update datasets set retrieval_model = '%s' where id='%s';" % (payload, DATASET_ID))


def write_workflow(g):
    import base64
    b = base64.b64encode(json.dumps(g, ensure_ascii=False).encode("utf-8")).decode()
    psql("update workflows set graph = convert_from(decode('%s','base64'),'UTF8')::jsonb "
         "where id='%s';" % (b, workflow_id()))


def load_graph():
    return json.loads(psql("select graph from workflows where id='%s';" % workflow_id()))


def apply_sets(sets):
    """写参数。

    键名带 `node.` 前缀表示改**检索节点级**配置，不带则改**知识库级**。
    必须显式区分：`score_threshold` 在 datasets.retrieval_model 和
    节点的 multiple_retrieval_config 里**都存在**，本机实测只改知识库级时
    运行时完全不生效（52 条用例引用条数与分数一字不差），真正被应用的是节点级。
    所以「改了没反应」最常见的原因就是只改了一处。
    """
    ds = json.loads(psql("select retrieval_model from datasets where id='%s';" % DATASET_ID))
    g = load_graph()
    changed_ds, changed_wf = [], []
    for kv in sets:
        k, v = kv.split("=", 1)
        val = json.loads(v)
        node_scoped = k.startswith("node.")
        if not node_scoped and k in ds:
            old = ds[k]
            ds[k] = val
            changed_ds.append((k, old, val))
            continue
        bare = k[5:] if node_scoped else k
        hit = False
        for n in g["nodes"]:
            d = n["data"]
            if d.get("type") == "knowledge-retrieval" and bare in d.get("multiple_retrieval_config", {}):
                old = d["multiple_retrieval_config"][bare]
                d["multiple_retrieval_config"][bare] = val
                changed_wf.append(("node.%s.%s" % (n["id"], bare), old, val))
                hit = True
            if d.get("type") == "llm" and bare in d.get("model", {}).get("completion_params", {}):
                old = d["model"]["completion_params"][bare]
                d["model"]["completion_params"][bare] = val
                changed_wf.append(("llm.%s" % bare, old, val))
                hit = True
        if not hit:
            raise KeyError("未知参数键: %s（节点级请用 node.<key>=<JSON值>）" % k)
    write_dataset(ds)
    write_workflow(g)
    return changed_ds, changed_wf


def apply_prompt(system_text):
    """整体替换 LLM 节点的 system 提示词。Dify 里每条 prompt 的 text 是一整段字符串，
    做局部字符串替换容易匹配到重复片段，所以按 role 定位整段覆盖。"""
    g = load_graph()
    old = None
    for n in g["nodes"]:
        if n["data"].get("type") != "llm":
            continue
        for t in n["data"].get("prompt_template", []):
            if t.get("role") == "system":
                old = t.get("text")
                t["text"] = system_text
    if old is None:
        raise RuntimeError("未找到 LLM 节点的 system 提示词")
    write_workflow(g)
    return old


def dump_prompt(path):
    g = load_graph()
    for n in g["nodes"]:
        if n["data"].get("type") != "llm":
            continue
        for t in n["data"].get("prompt_template", []):
            if t.get("role") == "system":
                io.open(path, "w", encoding="utf-8").write(t.get("text") or "")
                return True
    return False


def verify():
    st = read_state()
    return st["dataset_retrieval_model"]


def _write_snapshot(st, path):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    json.dump(st, io.open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--set", nargs="+", help="key=value，值按 JSON 解析")
    ap.add_argument("--restore", help="回滚到某个快照文件")
    ap.add_argument("--snapshot", help="把当前状态写到文件")
    ap.add_argument("--dump-prompt", help="导出当前 system 提示词到文件")
    ap.add_argument("--set-prompt", help="用文件里的文本整体替换 system 提示词")
    args = ap.parse_args()

    if args.dump_prompt:
        ok = dump_prompt(args.dump_prompt)
        print("已导出: %s" % args.dump_prompt if ok else "未找到 system 提示词")
        return 0

    if args.set_prompt:
        st = read_state()
        text = io.open(args.set_prompt, encoding="utf-8").read()
        old = apply_prompt(text)
        print("--- 旧提示词 ---\n%s\n--- 新提示词 ---\n%s" % (old, text))
        if args.snapshot:
            _write_snapshot(st, args.snapshot)
            print("\n改前快照: %s" % args.snapshot)
        return 0

    if args.restore:
        st = json.load(io.open(args.restore, encoding="utf-8"))
        write_dataset(st["dataset_retrieval_model"])
        g = load_graph()
        for saved in st["nodes"]:
            for n in g["nodes"]:
                if n["id"] != saved["node"]:
                    continue
                if saved.get("multiple_retrieval_config") is not None:
                    n["data"]["multiple_retrieval_config"] = saved["multiple_retrieval_config"]
                if saved.get("completion_params") is not None:
                    n["data"]["model"]["completion_params"] = saved["completion_params"]
                if saved.get("prompt_template") is not None:
                    n["data"]["prompt_template"] = [
                        {"role": t.get("role"), "text": t.get("text")}
                        for t in saved["prompt_template"]]
        write_workflow(g)
        print("已回滚 <- %s" % args.restore)
        print(json.dumps(verify(), ensure_ascii=False))
        return 0

    if args.set:
        st = read_state()
        ds_changes, wf_changes = apply_sets(args.set)
        for name, old, new in ds_changes:
            print("datasets.retrieval_model.%s : %s -> %s" % (name, old, new))
        for name, old, new in wf_changes:
            print("%s : %s -> %s" % (name, old, new))
        print("\n回读(知识库级):")
        print(json.dumps(verify(), ensure_ascii=False))
        if args.snapshot:
            _write_snapshot(st, args.snapshot)
            print("\n改前快照: %s" % args.snapshot)
        return 0

    if args.snapshot:
        st = read_state()
        _write_snapshot(st, args.snapshot)
        print("快照: %s" % args.snapshot)

    print(json.dumps(read_state(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
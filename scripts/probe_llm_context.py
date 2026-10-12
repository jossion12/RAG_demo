"""临时诊断：插桩 LLM 节点的 _fetch_context，打印实际拿到的变量类型。

只在容器内跑，用完还原（脚本自带还原逻辑）。

为什么不继续读源码猜：_fetch_context 有三条「什么都不发」或「发空」的分支
（返回 None / StringSegment / 非 ArraySegment），光看代码无法区分是哪一条。
"""

import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET = "/app/api/.venv/lib/python3.12/site-packages/graphon/nodes/llm/node.py"
BAK = TARGET + ".bak_probe"

# 在 _fetch_context 入口插入诊断打印
OLD = '''    ) -> Generator[RunRetrieverResourceEvent, None, None]:
        context_value_variable = self._get_context_value_variable(node_data)
        if context_value_variable is None:
            return'''
NEW = '''    ) -> Generator[RunRetrieverResourceEvent, None, None]:
        context_value_variable = self._get_context_value_variable(node_data)
        import os as _os
        try:
            _v = context_value_variable
            print("[PROBE] ctx.enabled=%r selector=%r type=%r value_type=%r" % (
                node_data.context.enabled,
                getattr(node_data.context.variable_selector, "value_selector", node_data.context.variable_selector),
                type(_v).__name__,
                type(getattr(_v, "value", None)).__name__,
            ), flush=True)
            if _v is not None:
                _val = getattr(_v, "value", None)
                if isinstance(_val, list):
                    print("[PROBE] list len=%d first_item_type=%s first_keys=%s" % (
                        len(_val), type(_val[0]).__name__ if _val else "N/A",
                        list(_val[0].keys()) if _val and isinstance(_val[0], dict) else "N/A"), flush=True)
                else:
                    print("[PROBE] value repr=%.200r" % (_val,), flush=True)
        if context_value_variable is None:
            return'''


def run(cmd):
    return subprocess.run(cmd, capture_output=True, shell=False)


def main() -> int:
    r = subprocess.run(["docker", "exec", "docker-api-1", "cp",
                        TARGET, f"/tmp/llm_node.py.bak"], capture_output=True)
    print("[*] 备份节点文件:", r.stdout.decode().strip() or r.stderr.decode().strip()[:200])

    p = subprocess.run(["docker", "exec", "docker-api-1", "python", "-c", f"""
import io
p = r'{TARGET}'
s = io.open(p, encoding='utf-8').read()
old = {OLD!r}
new = {NEW!r}
assert s.count(old) == 1, 'anchor count=%d' % s.count(old)
io.open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))
print('patched')
"""], capture_output=True)
    print(p.stdout.decode("utf-8", "replace").strip())
    if p.returncode != 0:
        print("[x] patch 失败:", p.stderr.decode("utf-8", "replace")[:600])
        return 2

    print("[*] 重启 api 容器使改动生效…")
    subprocess.run(["docker", "restart", "docker-api-1"], capture_output=True)
    time.sleep(25)
    print("[*] 完成。现在跑一次 quick_probe，再跑本脚本 --revert 还原。")
    return 0


def revert() -> int:
    p = subprocess.run(["docker", "exec", "docker-api-1", "python", "-c", f"""
import io
p = r'{TARGET}'
new = {NEW!r}
old = {OLD!r}
s = io.open(p, encoding='utf-8').read()
if new in s:
    io.open(p, 'w', encoding='utf-8').write(s.replace(new, old, 1))
    print('reverted in place')
else:
    print('already clean')
"""], capture_output=True)
    print(p.stdout.decode("utf-8", "replace").strip() or p.stderr.decode("utf-8", "replace")[:400])
    subprocess.run(["docker", "restart", "docker-api-1"], capture_output=True)
    time.sleep(20)
    print("[*] api 容器已重启并还原")
    return 0


if __name__ == "__main__":
    sys.exit(revert() if "--revert" in sys.argv else main())
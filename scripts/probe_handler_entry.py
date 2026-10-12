"""括号式插桩：在 handle_retriever_resources 入口打点，判定事件有没有到达管线末端。

前面已确认：
  - _fetch_context 拿到了 ArrayObjectVariable（2 条，带 _source=knowledge）
  - _convert_to_original_retriever_resource 对真实 item 能正确产出资源字典
所以事件「应当」被 yield。这里只在终点打一个点，就能二分出：
  终点有打印 -> 是 metadata 落库/序列化丢的
  终点没打印 -> 是 graphon _collect 或 runner 分发丢的

用法：
    C:\\Python312\\python.exe scripts\\probe_handler_entry.py
    C:\\Python312\\python.exe scripts\\probe_handler_entry.py --revert
"""

import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET = "/app/api/core/app/task_pipeline/message_cycle_manager.py"
BAK = "/tmp/message_cycle_manager.py.bak"

OLD = '''    def handle_retriever_resources(self, event: QueueRetrieverResourcesEvent):
        """'''
NEW = '''    def handle_retriever_resources(self, event: QueueRetrieverResourcesEvent):
        _af = getattr(self._application_generate_entity.app_config, "additional_features", "<<MISSING>>")
        print("[PROBE2] entered n=%d additional_features=%r show_retrieve_source=%r" % (
            len(event.retriever_resources or []), _af,
            getattr(_af, "show_retrieve_source", "<<NOATTR>>")), flush=True)
        """'''


def patch(apply: bool) -> int:
    src = TARGET if apply else BAK
    code = f"""
import io, shutil, sys
p = {TARGET!r}
old = {OLD!r}
new = {NEW!r}
if {apply!r}:
    shutil.copy(p, {BAK!r})
else:
    shutil.copy({BAK!r}, p)
    print('restored from bak')
    sys.exit(0)
s = io.open(p, encoding='utf-8').read()
assert s.count(old) == 1, 'anchor count=%d' % s.count(old)
io.open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))
import ast; ast.parse(io.open(p, encoding='utf-8').read())
print('patched + syntax OK')
"""
    p = subprocess.run(["docker", "exec", "docker-api-1", "python", "-c", code], capture_output=True)
    print(p.stdout.decode("utf-8", "replace").strip())
    if p.returncode != 0:
        print("[x]", p.stderr.decode("utf-8", "replace")[-800:])
        return 2
    print("[*] 重启 api 容器…")
    subprocess.run(["docker", "restart", "docker-api-1"], capture_output=True)
    time.sleep(30)
    return 0


if __name__ == "__main__":
    sys.exit(patch(apply="--revert" not in sys.argv))
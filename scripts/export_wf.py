"""宿主机侧编排：安全导出草稿图 -> 送进 api 容器 -> 用 Dify 的 schema 校验。

为什么不用 PowerShell 的 `docker exec ... > file`：
  PowerShell 会用控制台编码（中文 Windows 下通常 GBK）重新编码原生命令的
  UTF-8 输出，中文被截断成半个字符，导出的 JSON 直接解析失败。
  这里全程走 subprocess 的字节通道（capture_output=True -> bytes -> utf-8），
  编码不经过任何文本管道。

写入方向（fix_workflow_graph.py --apply）用的是 base64，ASCII 安全，
所以导出损坏不代表数据库内容损坏——校验要从数据库重新取，不能用本地副本。
"""

import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "validate_workflow_graph.py"


def run(args, **kw):
    return subprocess.run(args, capture_output=True, **kw)


def main() -> int:
    # 1) 导出（字节通道）
    p = run(["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
             "-t", "-A", "-c", "select graph from workflows where version='draft';"])
    if p.returncode != 0:
        print("[x] psql 失败:", p.stderr.decode("utf-8", "replace")[:300])
        return 2

    text = p.stdout.decode("utf-8").strip()
    if not text:
        print("[x] 草稿为空")
        return 2

    tmp = ROOT / "_wf_verify.json"
    tmp.write_text(text, encoding="utf-8")

    # 2) 送进容器
    if run(["docker", "cp", str(tmp), "docker-api-1:/tmp/wf_fixed.json"]).returncode != 0:
        print("[x] docker cp 失败")
        return 2
    if run(["docker", "cp", str(SCRIPT), "docker-api-1:/tmp/validate_wf.py"]).returncode != 0:
        print("[x] docker cp 脚本失败")
        return 2

    # 3) 容器内校验
    r = run(["docker", "exec", "-e", "PYTHONIOENCODING=utf-8",
             "docker-api-1", "python", "/tmp/validate_wf.py"])
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    err = r.stderr.decode("utf-8", "replace").strip()
    if err:
        sys.stdout.write("\n[stderr]\n" + err + "\n")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())

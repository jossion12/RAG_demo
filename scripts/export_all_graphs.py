"""宿主机编排：导出全部工作流版本 -> 送进 api 容器 -> 用 Dify schema 校验。

为什么不用 PowerShell 的 `docker exec ... > file`：
  PowerShell 会用控制台编码（中文 Windows 下通常 GBK）重新编码原生命令的
  UTF-8 输出，中文被截断成半个字符，JSON 直接解析失败。
  这里全程走 subprocess 字节通道，编码不经过任何文本管道。
"""

import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "scripts" / "verify_all_versions.py"
TMP = ROOT / "_all_graphs.json"

SQL = ("select version, graph from workflows "
       "order by version_number nulls last, version;")
SEP = "\x1f"  # ASCII 单元分隔符，避免与 JSON 内容冲突


def run(args):
    return subprocess.run(args, capture_output=True)


def main() -> int:
    p = run(["docker", "exec", "docker-db_postgres-1", "psql", "-U", "postgres", "-d", "dify",
             "-t", "-A", "-F", SEP, "-c", SQL])
    if p.returncode != 0:
        print("[x] psql:", p.stderr.decode("utf-8", "replace")[:300])
        return 2

    rows = []
    for line in p.stdout.decode("utf-8").strip().splitlines():
        if not line.strip():
            continue
        ver, graph = line.split(SEP, 1)
        rows.append([ver, graph])

    TMP.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
    print(f"[*] 已导出 {len(rows)} 个版本 -> {TMP.name}")

    for src, dst in [(str(TMP), "docker-api-1:/tmp/all_graphs.json"),
                     (str(VALIDATOR), "docker-api-1:/tmp/verify_all.py")]:
        if run(["docker", "cp", src, dst]).returncode != 0:
            print(f"[x] docker cp 失败: {dst}")
            return 2

    r = run(["docker", "exec", "-e", "PYTHONIOENCODING=utf-8",
             "docker-api-1", "python", "/tmp/verify_all.py"])
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    err = r.stderr.decode("utf-8", "replace").strip()
    if err:
        sys.stdout.write("\n[stderr]\n" + err + "\n")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())

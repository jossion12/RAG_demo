"""
Dify 本地自托管版：重置登录密码（忘记密码时的恢复工具）。

背景
----
Dify 自托管默认没有配 SMTP，点「忘记密码」收不到邮件，只能直接改库。
本脚本用 Dify 自己的算法重算密码哈希并写回 accounts 表。

算法必须与 Dify 完全一致（api/libs/password.py）：
    salt  = secrets.token_bytes(16)
    dk    = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 10000)
    hash  = base64(binascii.hexlify(dk))     # 注意是 base64(hex)，不是 base64(原始字节)
    salt  = base64(salt)

密码规则（Dify 自身校验）：
    必须同时包含字母和数字，且长度 >= 8

用法（在你自己�� PowerShell 里运行，密码只在本机输入，不会外传）
    & "C:\\Python312\\python.exe" scripts\\reset_dify_password.py
    & "C:\\Python312\\python.exe" scripts\\reset_dify_password.py someone@example.com

注意：把本脚本放进版本库前请确认不含真实密码。本脚本从不落盘密码。
"""

import base64
import binascii
import getpass
import hashlib
import re
import secrets
import subprocess
import sys

DB_CONTAINER = "docker-db_postgres-1"
DB_NAME = "dify"
DB_USER = "postgres"
PASSWORD_PATTERN = r"^(?=.*[a-zA-Z])(?=.*\d).{8,}$"


def valid_password(password: str) -> None:
    if re.match(PASSWORD_PATTERN, password) is None:
        raise ValueError("密码必须同时包含字母和数字，且长度不少于 8 位。")


def hash_password(password: str, salt: bytes) -> bytes:
    """与 Dify libs.password.hash_password 逐字节一致。"""
    return binascii.hexlify(hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 10000))


def run_psql(sql: str) -> str:
    """在 Dify 的 postgres 容器里执行 SQL，返回 stdout。"""
    proc = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-t", "-A", "-c", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 执行失败:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def list_accounts() -> list[tuple[str, str]]:
    out = run_psql("select email, status from accounts order by created_at;")
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


def main() -> int:
    print("=" * 62)
    print(" Dify 密码重置工具（本地自托管）")
    print("=" * 62)

    try:
        accounts = list_accounts()
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 无法连接 Dify 数据库: {exc}")
        print("    确认 Dify 容器已启动：docker ps | findstr postgres")
        return 2

    if not accounts:
        print("[x] 数据库里没有任何账号。")
        return 2

    print("\n当前账号：")
    for idx, (email, status) in enumerate(accounts, 1):
        print(f"  {idx}) {email}   [{status}]")

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if target is None:
        if len(accounts) == 1:
            target = accounts[0][0]
            print(f"\n将重置唯一账号：{target}")
        else:
            print("\n存在多个账号，请用参数指定邮箱：")
            print("    python reset_dify_password.py someone@example.com")
            return 2
    else:
        if not any(e == target for e, _ in accounts):
            print(f"\n[x] 账号不存在：{target}")
            return 2
        print(f"\n将重置账号：{target}")

    print("\n请输入新密码（输入时不显示字符）：")
    try:
        pwd1 = getpass.getpass("  新密码    > ")
        pwd2 = getpass.getpass("  再输一次  > ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 1

    if pwd1 != pwd2:
        print("\n[x] 两次输入不一致，已取消。")
        return 1

    try:
        valid_password(pwd1)
    except ValueError as exc:
        print(f"\n[x] {exc}")
        return 1

    salt = secrets.token_bytes(16)
    pwd_hash = base64.b64encode(hash_password(pwd1, salt)).decode()
    pwd_salt = base64.b64encode(salt).decode()

    # base64 字母表是 A-Za-z0-9+/=，不含引号，可安全拼接进 SQL 字面量
    sql = (
        f"update accounts set password='{pwd_hash}', password_salt='{pwd_salt}' "
        f"where email='{target}' returning email;"
    )
    try:
        result = run_psql(sql)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[x] 写入失败：{exc}")
        return 2

    if not result:
        print("\n[x] 没有匹配到账号，未做修改。")
        return 2

    print(f"\n[OK] 已重置：{target}")
    print("     现在可以回 Dify 登录页用新密码登录： http://localhost/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
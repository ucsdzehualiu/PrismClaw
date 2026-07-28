"""PrismClaw 启动前/退出后清理脚本。

1. 按端口 8765 找到并杀掉旧 server 进程（解析 netstat 输出，正则可靠，无 cmd 转义问题）
2. 递归删除所有 __pycache__，强制加载最新 .py 代码

用法：python cleanup.py
"""
import os
import re
import shutil
import subprocess

PORT = 8765


def kill_port_occupant() -> None:
    """杀掉占用 8765 端口的进程（排除 :87650 这类相邻端口）。"""
    try:
        proc = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = proc.stdout
    except Exception as exc:  # pragma: no cover - 环境异常
        print(f"  [cleanup] netstat 调用失败: {exc}")
        return

    killed = False
    for line in out.splitlines():
        # 只处理处于 LISTENING 且本地端口为 :8765 的行
        if "LISTENING" not in line:
            continue
        # 本地端口形如 0.0.0.0:8765，用负向预查排除 87650 等相邻端口
        if not re.search(rf":{PORT}(?!\d)", line):
            continue
        m = re.search(r"(\d+)\s*$", line.strip())
        if not m:
            continue
        pid = m.group(1)
        if pid == "0":
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", pid, "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            print(f"  [cleanup] killed old server PID {pid} on port {PORT}")
            killed = True
        except Exception as exc:  # pragma: no cover
            print(f"  [cleanup] kill PID {pid} 失败: {exc}")
    if not killed:
        print(f"  [cleanup] no process found on port {PORT}")


def clear_pycache() -> None:
    """递归删除所有 __pycache__ 目录。"""
    count = 0
    for root, dirs, _files in os.walk("."):
        if "__pycache__" in dirs:
            target = os.path.join(root, "__pycache__")
            try:
                shutil.rmtree(target)
                count += 1
            except Exception:  # pragma: no cover
                pass
    if count:
        print(f"  [cleanup] removed {count} __pycache__ dir(s)")
    else:
        print("  [cleanup] no __pycache__ to remove")


if __name__ == "__main__":
    print(f"[*] Cleaning port {PORT}...")
    kill_port_occupant()
    print("[*] Clearing __pycache__...")
    clear_pycache()
    print("[*] Cleanup done.")

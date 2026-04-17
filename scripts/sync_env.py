"""手动同步 .env 文件 — 主项目 ↔ worktree

正常情况下不需要运行此脚本（settings.py 已自动处理）。
仅当你想强制把主项目 .env 的内容物理复制到 worktree 时使用。

用法：
    python scripts/sync_env.py             # 主 → worktree
    python scripts/sync_env.py --check     # 仅检查是否一致
    python scripts/sync_env.py --reverse   # worktree → 主（谨慎使用）
"""
import sys
import os
import shutil
import argparse
import hashlib
from pathlib import Path

# 强制 UTF-8 输出（Windows GBK 不支持中文/emoji）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def get_main_root() -> Path | None:
    """从 worktree 反推主项目根目录"""
    cur_root = Path(__file__).resolve().parent.parent
    git_marker = cur_root / ".git"
    if not git_marker.is_file():
        return None  # 不是 worktree
    try:
        content = git_marker.read_text(encoding="utf-8").strip()
        if not content.startswith("gitdir:"):
            return None
        gitdir = content.split(":", 1)[1].strip()
        return Path(gitdir).parent.parent.parent  # 主项目根
    except Exception:
        return None


def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="仅检查，不复制")
    p.add_argument("--reverse", action="store_true", help="worktree → 主（谨慎）")
    args = p.parse_args()

    cur_root = Path(__file__).resolve().parent.parent
    main_root = get_main_root()

    if main_root is None:
        print("✓ 当前不在 worktree 中，无需同步")
        return 0

    main_env = main_root / ".env"
    worktree_env = cur_root / ".env"

    if not main_env.exists():
        print(f"✗ 主项目 .env 不存在: {main_env}")
        return 1

    main_hash = file_hash(main_env)
    worktree_hash = file_hash(worktree_env)

    print(f"主项目 .env:    {main_env}")
    print(f"  hash: {main_hash[:8]}, size: {main_env.stat().st_size} bytes")
    print(f"Worktree .env: {worktree_env}")
    if worktree_env.exists():
        print(f"  hash: {worktree_hash[:8]}, size: {worktree_env.stat().st_size} bytes")
    else:
        print(f"  (不存在)")
    print()

    if main_hash == worktree_hash:
        print("✓ 两个 .env 已一致，无需同步")
        return 0

    if args.check:
        print("⚠ 两个 .env 内容不一致")
        return 2

    src, dst = (worktree_env, main_env) if args.reverse else (main_env, worktree_env)
    label = "worktree → 主项目" if args.reverse else "主项目 → worktree"
    shutil.copy2(src, dst)
    print(f"✓ 已同步 {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

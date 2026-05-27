"""PostToolUse hook: auto python syntax check on .py edits."""
import sys, json, ast, os

def main():
    try:
        data = json.load(sys.stdin)
        path = data.get("tool_input", {}).get("file_path", "")
    except Exception:
        sys.exit(0)
    if not path.endswith(".py"):
        sys.exit(0)
    if not os.path.exists(path):
        sys.exit(0)
    try:
        ast.parse(open(path, encoding="utf-8").read())
        sys.exit(0)
    except SyntaxError as e:
        print(f"SYNTAX ERROR in {os.path.basename(path)}:{e.lineno}: {e.msg}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        sys.exit(0)

if __name__ == "__main__":
    main()

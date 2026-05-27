"""PreToolUse hook: block Edit/Write on secret files."""
import sys, json

def main():
    try:
        data = json.load(sys.stdin)
        path = data.get("tool_input", {}).get("file_path", "")
    except Exception:
        sys.exit(0)
    SECRET_PATTERNS = [".env", "credentials.json", "secrets.yaml", "secrets.json"]
    if any(p in path.lower() for p in SECRET_PATTERNS):
        print(f"BLOCKED: refusing to edit secret file {path}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()

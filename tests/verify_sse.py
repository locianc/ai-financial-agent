"""Phase 19 live 验证脚本：/health + POST /chat/stream SSE 流式请求。

直接运行（无需 pytest）：python tools/verify_sse.py [port]
"""
import json
import sys
import urllib.request

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else 8010}"
MESSAGE = sys.argv[2] if len(sys.argv) > 2 else "今天上证指数表现如何？请用一句话概括。"
TIMEOUT = float(sys.argv[3]) if len(sys.argv) > 3 else 90

# 1) /health
with urllib.request.urlopen(f"{BASE}/health", timeout=5) as resp:
    print("=== /health ===")
    print(resp.read().decode("utf-8"))

# 2) POST /chat/stream SSE
body = json.dumps({"message": MESSAGE, "session_id": None}).encode("utf-8")
req = urllib.request.Request(
    f"{BASE}/chat/stream",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
print("\n=== POST /chat/stream ===")
event_count = 0
token_chars = 0
with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
    print(f"HTTP {resp.status} content-type={resp.headers.get('Content-Type')}")
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        event_count += 1
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
                kind = payload.get("type", "?")
                if kind == "token":
                    token_chars += len(payload.get("content", ""))
                print(f"[{event_count}] {kind}: {json.dumps(payload, ensure_ascii=False)[:200]}")
            except json.JSONDecodeError:
                print(f"[{event_count}] raw: {line[:200]}")

print(f"\n共 {event_count} 个事件，token 累计 {token_chars} 字符")
print("SSE 流式验证完成")

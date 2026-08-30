"""Phase 19C 验证：极简反代模拟 nginx.conf 的关键 location。

模拟两个 location：
  /api/*            -> 去掉 /api 前缀转发到后端（nginx 中 proxy_pass http://backend:8000/）
  /chat/stream      -> 原样透传，不缓冲（nginx 中 proxy_buffering off）

直接运行：python tools/verify_nginx_proxy.py [listen_port] [backend_port]
"""
import http.server
import socketserver
import sys
import urllib.request

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
BACKEND = f"http://127.0.0.1:{sys.argv[2] if len(sys.argv) > 2 else 8010}"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self, raw_path: str) -> None:
        # 与 nginx location 匹配语义一致：仅 /api/ 开头去前缀，/chat/stream 原样透传
        if raw_path.startswith("/api/"):
            backend_url = BACKEND + raw_path[len("/api"):]
        else:
            backend_url = BACKEND + raw_path
        req = urllib.request.Request(
            backend_url,
            data=self.rfile.read(int(self.headers.get("Content-Length", 0))),
            headers={"Content-Type": "application/json"},
            method=self.command,
        )
        try:
            upstream = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            self.send_response(exc.code)
            self.end_headers()
            self.wfile.write(exc.read())
            return
        self.send_response(upstream.status)
        for k, v in upstream.headers.items():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        # nginx SSE 关键配置：Connection "" + 不缓冲
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            chunk = upstream.read(4096)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()  # 模拟 proxy_buffering off 的逐块下发

    def do_GET(self):
        self._proxy(self.path)

    def do_POST(self):
        self._proxy(self.path)

    def log_message(self, fmt, *args):
        print(f"[proxy] {self.command} {self.path} -> {fmt % args}", flush=True)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"[proxy] listening on 127.0.0.1:{LISTEN_PORT}, backend={BACKEND}", flush=True)
    with Server(("127.0.0.1", LISTEN_PORT), Handler) as srv:
        srv.serve_forever()

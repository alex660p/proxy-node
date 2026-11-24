"""
app-nodara.py - Nodaroo / Nodara 专版代理节点
用 Python 标准库, 无第三方依赖, Nodara 默认命令就是 python app.py

Nodara 关键点:
- 不是 VPS, 没有 root, 只能跑 Python 网站 / HTTP 应用
- 监听端口必须用环境变量 PORT, 默认 8080, 面板会注入
- 健康检查默认是 /, 必须返回 200, 否则显示 Can't reach server
- 对外只有一个免费地址 (HTTPS), 没有单独暴露的 TCP 代理端口,
  所以除了正向代理, 这里多加了网页代理模式:
  访问 https://你的免费地址/proxy?url=https://httpbin.org/ip
"""
import base64
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import http.client

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")

# 反向代理模式: 设置 TARGET_URL 后, 所有请求都转发到该目标
# 例: TARGET_URL=https://httpbin.org python3 app.py
TARGET_URL = os.environ.get("TARGET_URL", "").rstrip("/")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-connection",
    "te", "trailer", "proxy-authorization",
    "proxy-authenticate", "upgrade",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{self.client_address[0]}] {fmt % args}", flush=True)

    # ---------- 认证 ----------
    def _check_auth(self):
        if not PROXY_USER:
            return True
        # 浏览器访问网页代理时用 Authorization, curl -x 时用 Proxy-Authorization
        auth = self.headers.get("Proxy-Authorization", "") or self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, _, pwd = decoded.partition(":")
            return user == PROXY_USER and pwd == PROXY_PASS
        except Exception:
            return False

    def _require_auth(self):
        self.send_response(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 入口 ----------
    def do_GET(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_PUT(self): self._proxy()
    def do_DELETE(self): self._proxy()
    def do_PATCH(self): self._proxy()
    def do_HEAD(self): self._proxy()
    def do_OPTIONS(self): self._proxy()

    def do_CONNECT(self):
        """HTTPS 隧道: CONNECT example.com:443"""
        if not self._check_auth():
            return self._require_auth()
        host, _, port = self.path.partition(":")
        port = int(port or 443)
        try:
            remote = socket.create_connection((host, port), timeout=10)
        except Exception as e:
            self.send_error(502, f"CONNECT failed: {e}")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        # 双向转发
        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(8192)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try: dst.shutdown(socket.SHUT_WR)
                except Exception: pass
        t = threading.Thread(target=pipe, args=(self.connection, remote), daemon=True)
        t.start()
        pipe(remote, self.connection)
        remote.close()

    def _proxy(self):
        # 健康检查免鉴权：Nodara 面板是无鉴权访问 / 做健康检查，
        # 若先鉴权会返回 407，面板会判为 Can't reach server、一直无法 Running。
        # / 和 /health 只返回状态页，不代理任何流量；/proxy 与正向代理仍需鉴权。
        if self.path == "/health" and not TARGET_URL:
            body = b"ok - proxy node running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/" and not TARGET_URL:
            body = b'''<html><body><h2>Proxy node running</h2>
<form action="/proxy"><input name="url" size="60" placeholder="https://httpbin.org/ip">
<button>Go</button></form>
<p>Web proxy: /proxy?url=https://httpbin.org/ip</p></body></html>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._check_auth():
            return self._require_auth()

        # 网页代理模式 (Nodara 只能走 HTTPS 网站, 用这个最稳):
        #   /proxy?url=https://httpbin.org/ip
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/proxy":
            url = parse_qs(parsed_path.query).get("url", [""])[0]
            if url:
                self.path = url  # 复用下面的正向代理逻辑
            else:
                self.send_error(400, "missing ?url=")
                return

        # 解析目标 URL
        if TARGET_URL:
            target = TARGET_URL + self.path
        elif self.path.startswith("http://") or self.path.startswith("https://"):
            target = self.path  # 正向代理的绝对 URL
        else:
            host = self.headers.get("Host", "")
            target = f"http://{host}{self.path}"

        parsed = urlparse(target)
        scheme = parsed.scheme or "http"
        host = parsed.hostname
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        # 读取请求体
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        headers["Host"] = parsed.netloc

        try:
            conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(host, port, timeout=15)
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")
            return

        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if resp_body:
            self.wfile.write(resp_body)
        conn.close()


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    mode = f"反向代理 -> {TARGET_URL}" if TARGET_URL else "正向代理"
    auth = f"鉴权: {PROXY_USER}/******" if PROXY_USER else "鉴权: 未开启(仅内网使用)"
    print(f"代理节点已启动: http://{HOST}:{PORT}  [{mode}]  [{auth}]")
    print("测试: curl -x http://127.0.0.1:8080 http://httpbin.org/ip")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")

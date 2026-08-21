from __future__ import annotations

import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/simple/":
            self.send_error(404)
            return
        body = b"<!doctype html><a href='fixture-1.0.whl'>fixture</a>\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/opt/mock-package/cert.pem", "/opt/mock-package/key.pem")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()

# -*- coding: utf-8 -*-
"""API/MCP 库存连接器契约回归。"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from inventory_sources import ApiInventorySource, McpInventorySource


class ApiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/login":
            self.send_error(404)
            return
        payload = json.dumps({"data": {"token": "fixture-token"}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer fixture-token":
            self.send_error(401)
            return
        page = int(self.path.split("page=")[-1].split("&")[0]) if "page=" in self.path else 1
        rows = [{"code": "R-001", "name": "继电器", "qty": 4, "warehouse": "一号库",
                 "price": 1.3, "vendor": "演示供应商"}]
        if page == 2:
            rows = [{"code": "R-001", "name": "继电器", "qty": 6, "warehouse": "二号库",
                     "price": 1.1, "vendor": "演示供应商"}]
        if page > 2:
            rows = []
        payload = json.dumps({"data": {"items": rows}}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def write_mcp_server(path):
    script = r'''import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    if req["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif req["method"] == "tools/call":
        result = {"structuredContent": {"items": [{"ipn": "M-001", "name": "电机", "stock": 7, "location": "M库"}]}}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
'''
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(script)


def main():
    failures = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = {"inventory": {"source": "api", "api": {
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "login": {"path": "/login", "body": {"user": "fixture"},
                      "token_path": "data.token", "token_prefix": "Bearer"},
            "list_path": "/inventory", "data_path": "data.items",
            "pagination": {"type": "page", "size": 1, "max_pages": 5},
            "fields": {"ipn": "code", "model": "name", "stock": "qty",
                       "location": "warehouse", "unit_price": "price", "supplier": "vendor"}}}}
        parts = ApiInventorySource(cfg).load_parts()
        if len(parts) != 1 or parts[0]["unconfirmed_stock"] != 10:
            failures.append("API 分页或库存合并错误")
        if parts[0]["confirmed_stock"] != 0:
            failures.append("API 普通库存不应直接视为已确认")
        if parts[0]["unit_price"] != 1.1 or len(parts[0]["locations"]) != 2:
            failures.append("API 价格或库位映射错误")
    finally:
        server.shutdown()
        server.server_close()

    with tempfile.TemporaryDirectory(prefix="pipeline_mcp_") as tmp:
        script = os.path.join(tmp, "mcp_server.py")
        write_mcp_server(script)
        cfg = {"inventory": {"source": "mcp", "mcp": {
            "command": [sys.executable, script], "tool": "inventory.list",
            "data_path": "items", "fields": {"ipn": "ipn", "model": "name",
                                                  "stock": "stock", "location": "location"}}}}
        parts = McpInventorySource(cfg).load_parts()
        if len(parts) != 1 or parts[0]["ipn"] != "M-001":
            failures.append("MCP 工具结果解析错误")
        if parts[0]["unconfirmed_stock"] != 7 or parts[0]["confirmed_stock"] != 0:
            failures.append("MCP 普通库存审核语义错误")

    if failures:
        for failure in failures:
            print("FAIL: " + failure)
        return 1
    print("ALL API/MCP CONNECTOR CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

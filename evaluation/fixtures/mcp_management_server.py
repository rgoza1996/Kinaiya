"""Public synthetic JSON-RPC peer for management transport verification."""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def reply(request, calls):
    with calls.open("a") as output:
        output.write(json.dumps({"method": request.get("method")}) + "\n")
    if "id" not in request:
        return None
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic-management-peer", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in ("echo", "hidden_tool")
            ]
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32601, "message": "No tool execution in management fixture"},
        }
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls-log", required=True, type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.port is None:
        for line in sys.stdin:
            response = reply(json.loads(line), args.calls_log)
            if response is not None:
                print(json.dumps(response), flush=True)
        return

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            response = reply(
                json.loads(self.rfile.read(int(self.headers["Content-Length"]))), args.calls_log
            )
            if response is None:
                self.send_response(202)
                self.end_headers()
                return
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

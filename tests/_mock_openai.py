"""运行体认证用的假 OpenAI 兼容模型服务（进程内线程）：按脚本依次发起工具调用，不花 token、结果确定。

脚本是步骤列表，第 N 步在模型已收到 N 条工具结果时执行：{"pick": 工具名, "args": {...}} 或 {"say": 文本}。
pick 以 * 开头表示后缀匹配（*read_file 匹配 MCP 前缀的只读工具）；工具不在可用清单里时改为回复文本。
每次请求记录在 server.requests（工具清单、最后一条消息、已收到的工具结果数）。
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _pick(rule, names):
    if rule.startswith("*"):
        return next((n for n in names if n.endswith(rule[1:])), None)
    return rule if rule in names else None


def start(script):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"object": "list", "data": [{"id": "m", "object": "model"}]})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            msgs = req.get("messages") or []
            names = [t.get("function", {}).get("name", "") for t in req.get("tools") or []]
            done = sum(1 for m in msgs if m.get("role") == "tool")
            step = script[done] if done < len(script) else {"say": "done"}
            requests.append({"tools": names, "tool_results": done, "last": msgs[-1] if msgs else None})
            name = _pick(step["pick"], names) if "pick" in step else None
            if name:
                delta = {"role": "assistant", "content": None, "tool_calls": [{"index": 0, "id": f"call_{done}", "type": "function",
                         "function": {"name": name, "arguments": json.dumps(step.get("args") or {})}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": step.get("say") or "tool not available"}
                finish = "stop"
            base = {"id": f"c{time.time_ns()}", "object": "chat.completion.chunk", "created": int(time.time()), "model": "m"}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in ({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = requests
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def tool_results(server):
    """模型收到的工具结果文本（按顺序）。"""
    out = []
    for r in server.requests:
        last = r.get("last") or {}
        if last.get("role") == "tool":
            content = last.get("content")
            out.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
    return out

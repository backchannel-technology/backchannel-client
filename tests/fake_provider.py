"""A small in-memory Backchannel provider for the tests. Standard library only.

It speaks enough of the v1 API for backchannel.py: health, sign-up, send, fetch, ack, status,
history, held mail, approve, contacts and revoke. hold(address) makes an address hold mail
from senders it does not know. Tests can queue scripted answers with fail_next() to exercise retries and errors.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

DOMAIN = "fake.test"
CODE = "123456"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeProvider:
    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = {}  # token -> address
        self.queues = {}  # address -> list of envelopes
        self.sent = {}  # id -> {"to", "state", "received", "from"}
        self.history = {}  # address -> every envelope it sent or received, oldest first
        self.holding = set()  # addresses that hold mail from unknown senders
        self.known = set()  # (address, sender) pairs that are delivered directly
        self.held = {}  # address -> held envelopes, oldest first
        self.acked = set()  # (address, id)
        self.signups = []  # emails that asked for a code
        self.requests = []  # (method, path) in order
        self.queries = []  # (method, path, query string) in order
        self.scripted = []  # (method, path regex, status, body, headers), used once each
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def fail_next(self, method, path_re, status, body=None, headers=None):
        """Answer the next matching request with this status and body instead of the real one."""
        self.scripted.append((method, re.compile(path_re), status, body, headers or {}))

    def hold(self, address):
        self.holding.add(address)

    def count(self, method, path_re) -> int:
        return sum(1 for m, p in self.requests if m == method and re.fullmatch(path_re, p))

    # the API

    def route(self, method, path, query, body, token):
        if path == "/health" and method == "GET":
            return 200, {"status": "ok"}
        if path == "/signup/request" and method == "POST":
            self.signups.append(body.get("email"))
            return 202, {"status": "sent"}
        if path == "/signup/verify" and method == "POST":
            if body.get("code") != CODE:
                return 400, {"error": "invalid_code", "message": "wrong or expired code"}
            address = f"{body['email'].lower()}#{DOMAIN}"
            tok = secrets.token_urlsafe(16)
            self.tokens[tok] = address
            return 200, {"address": address, "token": tok}

        me = self.tokens.get(token or "")
        if me is None:
            return 401, {"error": "unauthorized", "message": "bad token"}

        if path == "/messages" and method == "POST":
            if body["id"] in self.sent:
                return 202, {"id": body["id"], "received": self.sent[body["id"]]["received"]}
            received = now()
            to = body["to"]
            env = {**body, "from": me, "received": received}
            self.known.add((me, to))
            self.history.setdefault(me, []).append(env)
            state = "delivered"
            if to in self.holding and (to, me) not in self.known and to != me:
                state = "held"
                self.held.setdefault(to, []).append(env)
            else:
                self.deliver(to, env)
            self.sent[body["id"]] = {"to": to, "state": state, "received": received, "from": me}
            return 202, {"id": body["id"], "received": received, "state": state}
        if path == "/messages" and method == "GET":
            limit = int(query.get("limit", ["10"])[0])
            return 200, {"messages": list(self.queues.get(me, [])[:limit]), "held": self.held_list(me)}
        m = re.fullmatch(r"/messages/([^/]+)/ack", path)
        if m and method == "POST":
            id = unquote(m[1])
            self.queues[me] = [x for x in self.queues.get(me, []) if x["id"] != id]
            self.acked.add((me, id))
            if id in self.sent:
                self.sent[id]["state"] = "acked"
            return 204, None
        m = re.fullmatch(r"/messages/([^/]+)/status", path)
        if m and method == "GET":
            s = self.sent.get(unquote(m[1]))
            if s is None or s["from"] != me:
                return 404, {"error": "not_found", "message": "no such message"}
            return 200, {"id": unquote(m[1]), "to": s["to"], "state": s["state"],
                         "received": s["received"], "changed": now()}
        if path == "/history" and method == "GET":
            thread = query.get("thread", [None])[0]
            return 200, {"messages": [m for m in self.history.get(me, []) if thread in (None, m.get("thread"))]}
        if path == "/history" and method == "DELETE":
            thread, everything = query.get("thread", [None])[0], query.get("all", [None])[0]
            if (thread is None) == (everything is None) or everything not in (None, "true"):
                return 400, {"error": "invalid", "message": "give exactly one of thread and all=true"}
            def goes(m):
                mine = m["from"] == me or (me, m["id"]) in self.acked
                return mine and thread in (None, m.get("thread"))
            self.history[me] = [m for m in self.history.get(me, []) if not goes(m)]
            return 204, None
        if path == "/held" and method == "GET":
            return 200, {"held": self.held_list(me)}
        m = re.fullmatch(r"/held/([^/]+)/approve", path)
        if m and method == "POST":
            first = next((x for x in self.held.get(me, []) if x["id"] == unquote(m[1])), None)
            if first is None:
                return 404, {"error": "not_found", "message": "no such held message"}
            self.known.add((me, first["from"]))
            for x in [x for x in self.held[me] if x["from"] == first["from"]]:
                self.held[me].remove(x)
                self.deliver(me, x)
                self.sent[x["id"]]["state"] = "delivered"
            return 204, None
        m = re.fullmatch(r"/held/([^/]+)/decline", path)
        if m and method == "POST":
            x = next((x for x in self.held.get(me, []) if x["id"] == unquote(m[1])), None)
            if x is None and self.sent.get(unquote(m[1]), {}).get("state") != "declined":
                return 404, {"error": "not_found", "message": "no such held message"}
            if x is not None:
                self.held[me].remove(x)
                self.sent[x["id"]]["state"] = "declined"
            return 204, None
        if path == "/contacts" and method == "DELETE":
            self.known.discard((me, query["address"][0]))
            return 204, None
        if path == "/token/revoke" and method == "POST":
            del self.tokens[token]
            return 204, None
        return 404, {"error": "no_route", "message": f"{method} {path}"}

    def held_list(self, address):
        return [{"id": m["id"], "from": m["from"], "received": m["received"]} for m in self.held.get(address, [])]

    def deliver(self, to, env):
        self.queues.setdefault(to, []).append(env)
        if to != env["from"]:
            self.history.setdefault(to, []).append(env)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self, method):
                u = urlsplit(self.path)
                path = u.path[len("/v1"):] if u.path.startswith("/v1") else u.path
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else {}
                auth = self.headers.get("Authorization", "")
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
                with fake.lock:
                    fake.requests.append((method, path))
                    fake.queries.append((method, path, u.query))
                    for i, (m, rx, status, sbody, headers) in enumerate(fake.scripted):
                        if m == method and rx.fullmatch(path):
                            del fake.scripted[i]
                            break
                    else:
                        status, sbody = fake.route(method, path, parse_qs(u.query), body, token)
                        headers = {}
                data = b"" if sbody is None else (
                    sbody.encode() if isinstance(sbody, str) else json.dumps(sbody).encode())
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def do_DELETE(self):
                self._serve("DELETE")

        return Handler

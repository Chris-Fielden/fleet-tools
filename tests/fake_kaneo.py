"""A tiny fake Kaneo: http.server on 127.0.0.1, in memory, records requests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeKaneo:
    def __init__(self, api_key: str, workspace_id: str = "ws1"):
        self.api_key = api_key
        self.workspace_id = workspace_id
        self.projects: list[dict] = []
        self.columns: dict[str, list[dict]] = {}
        self.tasks: dict[str, dict] = {}
        self.labels: list[dict] = []
        self.fail_labels = False             # every /labels request -> 500
        self.task_labels: dict[str, list[str]] = {}  # task id -> label ids
        self.requests: list[dict] = []
        self.fail_title: str | None = None   # POST /task with this in the title -> 500
        self.fail_column: str | None = None  # POST /column with this name -> 500 (once)
        self.fail_all = False                # every request -> 500, body echoes the key
        self.redirect_to: str | None = None  # every request -> 302 to this URL
        self._n = 0
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # lifecycle
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/api"

    @property
    def writes(self) -> list[dict]:
        return [r for r in self.requests if r["method"] != "GET"]

    def next_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    # routing: returns (status, json)
    def route(self, method: str, path: str, query: dict, body):
        parts = path.strip("/").split("/")
        if parts[0] == "labels":
            if self.fail_labels:
                return 500, {"error": "boom"}
            if len(parts) == 1 and method == "GET":
                return 200, [l for l in self.labels if l["workspaceId"] == query["workspaceId"][0]]
            if len(parts) == 1:
                lab = {"id": self.next_id("l"), "name": body["name"],
                       "color": body.get("color"), "workspaceId": body["workspaceId"]}
                self.labels.append(lab)
                return 200, lab
            if len(parts) == 3 and parts[2] in ("attach", "detach"):
                ids = self.task_labels.setdefault(body["taskId"], [])
                if parts[2] == "attach" and parts[1] not in ids:
                    ids.append(parts[1])
                if parts[2] == "detach" and parts[1] in ids:
                    ids.remove(parts[1])
                return 200, {"ok": True}
        if parts[0] == "tasks" and len(parts) == 3 and parts[2] == "labels":
            return 200, [l for l in self.labels if l["id"] in self.task_labels.get(parts[1], [])]
        if parts[0] == "project" and len(parts) == 1:
            if method == "GET":
                return 200, [p for p in self.projects if p["workspaceId"] == query["workspaceId"][0]]
            p = {"id": self.next_id("p"), "workspaceId": body["workspaceId"], "name": body["name"],
                 "slug": body["slug"]}
            self.projects.append(p)
            self.columns[p["id"]] = []
            return 200, p
        if parts[:2] == ["column", "reorder"] and len(parts) == 3 and method == "PUT":
            cols = self.columns[parts[2]]
            for item in body["columns"]:
                next(c for c in cols if c["id"] == item["id"])["position"] = item["position"]
            cols.sort(key=lambda c: c["position"])
            return 200, cols
        if parts[0] == "column" and len(parts) == 2 and method == "DELETE":
            for cols in self.columns.values():
                for c in cols:
                    if c["id"] == parts[1]:
                        if any(t["projectId"] == c["projectId"] and t["status"] == c["slug"]
                               for t in self.tasks.values()):
                            return 409, {"error": "The column still contains tasks"}
                        cols.remove(c)
                        return 200, c
            return 400, {"error": "unknown column"}
        if parts[0] == "column" and len(parts) == 2 and method == "PUT":
            for cols in self.columns.values():
                for c in cols:
                    if c["id"] == parts[1]:
                        c.update(body)
                        return 200, c
            return 404, {"error": "not found"}
        if parts[0] == "column" and len(parts) == 2:
            cols = self.columns[parts[1]]
            if method == "GET":
                return 200, sorted(cols, key=lambda c: c.get("position", 0))
            if self.fail_column == body["name"]:
                self.fail_column = None
                return 500, {"error": "boom"}
            c = {"id": self.next_id("c"), "projectId": parts[1], "name": body["name"],
                 "position": len(cols),
                 "slug": body["name"].lower().replace(" ", "-"), "isFinal": body.get("isFinal", False)}
            cols.append(c)
            return 200, c
        if parts[:2] == ["task", "tasks"] and len(parts) == 3:
            want = query.get("status", [None])[0]
            found = [t for t in self.tasks.values() if t["projectId"] == parts[2]
                     and (want is None or t["status"] == want)]
            return 200, {"data": {"columns": [{"id": "x", "tasks": found}],
                                  "archivedTasks": [], "plannedTasks": []}}
        if parts[0] == "task" and len(parts) == 2:
            if method == "POST":
                if self.fail_title and self.fail_title in body["title"]:
                    return 500, {"error": "boom"}
                t = {"id": self.next_id("t"), "projectId": parts[1], "position": 0, **body}
                self.tasks[t["id"]] = t
                return 200, t
            t = self.tasks.get(parts[1])
            if t is None:
                return 404, {"error": "not found"}
            if method == "PUT":
                t.update(body)
            return 200, t
        return 404, {"error": "no route"}

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                u = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.requests.append({
                    "method": self.command, "path": u.path.removeprefix("/api"),
                    "auth": self.headers.get("Authorization"), "body": body})
                if fake.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", fake.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if fake.fail_all:
                    status, payload = 500, {"error": f"internal, key was {fake.api_key}"}
                elif self.headers.get("Authorization") != f"Bearer {fake.api_key}":
                    status, payload = 401, {"error": "unauthorized"}
                else:
                    status, payload = fake.route(
                        self.command, u.path.removeprefix("/api"), parse_qs(u.query), body)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = _do

        return H

    def label_names(self, task_id: str) -> list[str]:
        return [l["name"] for l in self.labels if l["id"] in self.task_labels.get(task_id, [])]

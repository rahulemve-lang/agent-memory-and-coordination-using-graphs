"""
server.py - HTTP API over graphmem so n8n (or anything) can use the graph.

Every endpoint is POST + JSON, and every body carries "tenant" and "user" (these pick
the isolated FalkorDB graph). No dependencies beyond graphmem itself.

  POST /remember          {agent, text, importance?, entities?, relations?, facts?}
  POST /recall            {question, hops?, limit?}          -> {context, items, seeds}
  POST /fact/set          {subject, predicate, value, agent?, expected_version?}
  POST /decide            {agent, summary, based_on:["fact:<id>",...], task_id?}
  POST /review            {decision_id, agent, approved, reason?}
  POST /why               {decision_id}                      -> full provenance
  POST /stale             {}                                 -> decisions on changed facts
  POST /task/create_many  {from_agent, tasks:[{title, target?, payload?, after?:[idx]}]}
  POST /task/claim        {agent}                            -> {task|null}
  POST /task/complete     {task_id, agent, result?}
  GET  /health

Run:  FALKORDB_HOST=localhost GRAPHMEM_API_KEY=secret python server.py
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from graphmem import GraphMemory, Item, VersionConflict


def _need(body: dict, *keys):
    for k in keys:
        if body.get(k) in (None, ""):
            raise ValueError(f"missing field: {k}")
    return [body[k] for k in keys]


def _item(i: Item) -> dict:
    return {"tag": i.tag, "kind": i.kind, "id": i.id, "text": i.text, "hops": i.hops}


def _parse_refs(refs) -> list:
    """'fact:abc123' / {'kind':..,'id':..} -> Item. Lets an LLM cite tags verbatim."""
    out = []
    for r in refs or []:
        if isinstance(r, str):
            kind, _, rid = r.partition(":")
        else:
            kind, rid = r.get("kind"), r.get("id")
        if kind not in ("fact", "episode", "procedure") or not rid:
            raise ValueError(f"bad reference: {r!r}")
        out.append(Item(kind, rid, ""))
    return out


# ------------------------------------------------------------------ endpoints
def remember(m, b):
    _need(b, "agent", "text")
    ep = m.remember(b["agent"], b["text"], float(b.get("importance", 0.5)),
                    entities=b.get("entities"), relations=b.get("relations"),
                    facts=b.get("facts"))
    return {"episode_id": ep}


def recall(m, b):
    _need(b, "question")
    r = m.recall(b["question"], hops=int(b.get("hops", 2)), limit=int(b.get("limit", 8)))
    return {"seeds": r.seeds, "context": r.text(), "items": [_item(i) for i in r.items]}


def fact_set(m, b):
    _need(b, "subject", "predicate", "value")
    v = m.set_fact(b["subject"], b["predicate"], b["value"], agent=b.get("agent"),
                   expected_version=b.get("expected_version"))
    return {"version": v}


def decide(m, b):
    _need(b, "agent", "summary")
    did = m.record_decision(b["agent"], b["summary"], _parse_refs(b.get("based_on")),
                            task_id=b.get("task_id"))
    return {"decision_id": did}


def review(m, b):
    _need(b, "decision_id", "agent")
    return {"status": m.review_decision(b["decision_id"], b["agent"],
                                        bool(b.get("approved")), b.get("reason", ""))}


def why(m, b):
    _need(b, "decision_id")
    w = m.why(b["decision_id"])
    w["text"] = m.explain(b["decision_id"])
    return w


def stale(m, b):
    return {"stale": m.stale_decisions()}


def task_create_many(m, b):
    _need(b, "from_agent", "tasks")
    ids = []
    for i, t in enumerate(b["tasks"]):
        _need(t, "title")
        after = t.get("after", [])
        if any((not isinstance(a, int)) or a < 0 or a >= i for a in after):
            raise ValueError(f"task {i}: 'after' must reference EARLIER task indexes")
        ids.append(m.create_task(t["title"], b["from_agent"],
                                 depends_on=[ids[a] for a in after],
                                 target=t.get("target", ""), payload=t.get("payload")))
    return {"task_ids": ids}


def task_claim(m, b):
    _need(b, "agent")
    return {"task": m.claim_task(b["agent"])}


def task_complete(m, b):
    _need(b, "task_id", "agent")
    m.complete_task(b["task_id"], b["agent"], b.get("result", ""))
    return {"ok": True}


ROUTES = {"/remember": remember, "/recall": recall, "/fact/set": fact_set,
          "/decide": decide, "/review": review, "/why": why, "/stale": stale,
          "/task/create_many": task_create_many, "/task/claim": task_claim,
          "/task/complete": task_complete}


# --------------------------------------------------------------------- server
def make_server(gm, host="127.0.0.1", port=8080, api_key=None):
    cache, lock = {}, threading.Lock()

    def scope(tenant, user):
        with lock:
            if (tenant, user) not in cache:
                cache[(tenant, user)] = gm.scope(tenant, user)
            return cache[(tenant, user)]

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"ok": True}) if self.path == "/health" \
                else self._send(404, {"error": "not found"})

        def do_POST(self):
            if api_key and self.headers.get("X-API-Key") != api_key:
                return self._send(401, {"error": "bad or missing X-API-Key"})
            fn = ROUTES.get(self.path)
            if not fn:
                return self._send(404, {"error": f"unknown route {self.path}"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                tenant, user = _need(body, "tenant", "user")
                self._send(200, fn(scope(tenant, user), body))
            except VersionConflict as e:
                self._send(409, {"error": str(e)})
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self._send(400, {"error": f"{type(e).__name__}: {e}"})
            except Exception as e:  # keep n8n informed instead of dropping the socket
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt, *args):
            if os.environ.get("GRAPHMEM_LOG"):
                super().log_message(fmt, *args)

    return ThreadingHTTPServer((host, port), H)


if __name__ == "__main__":
    gm = GraphMemory(os.environ.get("FALKORDB_HOST", "localhost"),
                     int(os.environ.get("FALKORDB_PORT", "6379")))
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    key = os.environ.get("GRAPHMEM_API_KEY")
    if not key:
        print("WARNING: GRAPHMEM_API_KEY not set - API is open to anyone who can reach it")
    print(f"graphmem API on {host}:{port}")
    make_server(gm, host, port, key).serve_forever()

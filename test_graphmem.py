"""Integration tests - need FalkorDB on localhost:6379 (docker compose up -d).
Run:  python test_graphmem.py      (or: pytest -q)
"""
import os
import sys
import threading
import time
import uuid

from graphmem import GraphMemory, VersionConflict

HOST = os.environ.get("FALKORDB_HOST", "localhost")
PORT = int(os.environ.get("FALKORDB_PORT", "6379"))


def mem(tenant=None, user="u"):
    tenant = tenant or "t" + uuid.uuid4().hex[:8]
    return GraphMemory(HOST, PORT).scope(tenant, user)


def test_isolation():
    t = "t" + uuid.uuid4().hex[:8]
    a, b = mem(t, "alice"), mem(t, "bob")
    try:
        a.remember("x", "Acme secret", entities=[("Acme", "org")],
                   facts=[("Acme", "code", "alpha")])
        assert a.recall("Acme").of("fact")
        assert b.recall("Acme").items == [] and b.get_fact("Acme", "code") is None
    finally:
        a.erase(); b.erase()


def test_cas_and_history():
    m = mem()
    try:
        v1 = m.set_fact("Acme", "budget", "50k")
        assert v1 == 1
        assert m.set_fact("Acme", "budget", "50k") == 1          # idempotent
        assert m.set_fact("Acme", "budget", "80k", expected_version=1) == 2
        try:
            m.set_fact("Acme", "budget", "99k", expected_version=1)   # stale
            raise AssertionError("expected VersionConflict")
        except VersionConflict:
            pass
        assert m.get_fact("Acme", "budget").text.endswith("80k")
        assert [h.props["value"] for h in m.fact_history("Acme", "budget")] == ["50k", "80k"]
    finally:
        m.erase()


def test_concurrent_cas_is_exact():
    t = "t" + uuid.uuid4().hex[:8]
    m = mem(t)
    m.set_fact("counter", "n", "0")

    def worker():
        mm = mem(t)
        for _ in range(10):
            while True:
                cur = mm.get_fact("counter", "n")
                try:
                    mm.set_fact("counter", "n", str(int(cur.props["value"]) + 1),
                                expected_version=cur.version)
                    break
                except VersionConflict:
                    pass
    try:
        ts = [threading.Thread(target=worker) for _ in range(4)]
        [x.start() for x in ts]; [x.join() for x in ts]
        assert m.get_fact("counter", "n").props["value"] == "40"
    finally:
        m.erase()


def test_multihop_recall_needs_the_graph():
    m = mem()
    try:
        m.remember("a", "chain", entities=[("A", ""), ("B", ""), ("C", "")],
                   relations=[("A", "knows", "B"), ("B", "knows", "C")],
                   facts=[("C", "colour", "green")])
        texts = lambda r: [i.text for i in r.of("fact")]
        assert any("green" in x for x in texts(m.recall("tell me about A", hops=2)))
        assert not any("green" in x for x in texts(m.recall("tell me about A", hops=1)))
    finally:
        m.erase()


def test_tasks_respect_dependencies_and_claim_once():
    t = "t" + uuid.uuid4().hex[:8]
    m = mem(t)
    try:
        a = m.create_task("A", "planner")
        b = m.create_task("B", "planner", depends_on=[a])
        first = m.claim_task("w1")
        assert first["id"] == a and m.claim_task("w2") is None       # B blocked
        m.complete_task(a, "w1", "done-A")
        nxt = m.claim_task("w2")
        assert nxt["id"] == b and nxt["upstream"][0]["result"] == "done-A"

        for i in range(12):
            m.create_task(f"free{i}", "planner")
        got, lock = [], threading.Lock()

        def worker(n):
            mm = mem(t)
            while (c := mm.claim_task(n)):
                with lock:
                    got.append(c["id"])
        ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        [x.start() for x in ts]; [x.join() for x in ts]
        assert len(got) == 12 and len(set(got)) == 12
    finally:
        m.erase()


def test_decision_trace_and_staleness():
    m = mem()
    try:
        ep = m.remember("p", "Acme needs India-only data", entities=[("Acme", "org")],
                        facts=[("Acme", "residency", "India only")])
        r = m.recall("Acme", hops=0)
        fact = r.of("fact")[0]
        d = m.record_decision("res", "Use Mumbai", based_on=[fact])
        w = m.why(d)
        assert not w["evidence"][0]["stale"]
        assert w["evidence"][0]["derived_from"][0]["id"] == ep
        assert m.stale_decisions() == []
        m.set_fact("Acme", "residency", "EU ok")
        assert m.why(d)["evidence"][0]["stale"]
        assert [x["id"] for x in m.stale_decisions()] == [d]
    finally:
        m.erase()


def test_review_flow():
    m = mem()
    try:
        m.remember("p", "x", entities=[("Acme", "")], facts=[("Acme", "k", "v")])
        f = m.recall("Acme", hops=0).of("fact")[0]
        d = m.record_decision("res", "do it", based_on=[f])
        assert m.why(d)["status"] == "proposed"
        assert m.review_decision(d, "verifier", False, "contradicts k") == "rejected"
        w = m.why(d)
        assert w["status"] == "rejected" and w["reviews"][0]["reason"] == "contradicts k"
    finally:
        m.erase()


def test_forget_keeps_cited_episodes():
    m = mem()
    try:
        keep = m.remember("a", "old but cited", importance=0.1, entities=[("X", "")],
                          facts=[("X", "k", "v")])
        drop = m.remember("a", "old and useless", importance=0.1, entities=[("X", "")])
        m._q("MATCH (ep:Episode) SET ep.ts = $t", t=time.time() - 90 * 86400)
        assert m.forget(older_than_days=30, max_importance=0.3) == 1
        ids = [r[0] for r in m._q("MATCH (ep:Episode) RETURN ep.id")]
        assert ids == [keep] and drop not in ids
    finally:
        m.erase()


def test_erase():
    m = mem()
    m.remember("a", "hi", entities=[("Z", "")])
    m.erase()
    try:
        s = m.stats()
    except Exception:   # graph key no longer exists
        s = {}
    assert s == {}


if __name__ == "__main__":
    try:
        GraphMemory(HOST, PORT).db.list_graphs()
    except Exception as e:
        sys.exit(f"Cannot reach FalkorDB at {HOST}:{PORT} ({e}). Run: docker compose up -d")
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for f in fns:
        try:
            f(); print("PASS", f.__name__)
        except Exception as e:
            failed += 1; print("FAIL", f.__name__, "->", repr(e))
    sys.exit(1 if failed else 0)

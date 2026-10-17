"""
demo.py - the hackathon story, end to end. Needs FalkorDB running (docker compose up -d).

  Session 1: planner, researcher, coder coordinate through the task graph.
             The researcher's decision is traceable to the facts + episodes behind it.
  Session 2: a NEW process-like session. A fact changes; the graph flags the old
             decision as stale and the trace page shows exactly why.

Uses explicit structured data so it runs with no API key. For real agents, drop the
entities/relations/facts args and pass extractor=LLMExtractor(anthropic_call()).
"""
import os
from graphmem import GraphMemory
from viz import write_trace

HOST = os.environ.get("FALKORDB_HOST", "localhost")
PORT = int(os.environ.get("FALKORDB_PORT", "6379"))


def hr(t):
    print(f"\n=== {t} " + "=" * (60 - len(t)))


# ---------------------------------------------------------------- SESSION 1
gm = GraphMemory(HOST, PORT)
m = gm.scope("acme-corp", "priya")
m.erase()
m = gm.scope("acme-corp", "priya")          # fresh graph for a clean demo

hr("SESSION 1 - planner ingests the brief")
m.remember(
    "planner",
    "Acme wants a Q3 revenue dashboard. Their compliance team requires all customer "
    "data to stay in India.",
    importance=0.8,
    entities=[("Acme", "org"), ("Q3 Dashboard", "project"), ("Compliance Team", "org"),
              ("Postgres", "tool"), ("Mumbai Region", "place")],
    relations=[("Q3 Dashboard", "requested_by", "Acme"),
               ("Acme", "governed_by", "Compliance Team"),
               ("Q3 Dashboard", "can_use", "Postgres"),
               ("Postgres", "hosted_in", "Mumbai Region")],
    facts=[("Acme", "data residency", "India only")],
)
m.remember(
    "planner",
    "Infra note: Postgres is available in the Mumbai region and the Frankfurt region.",
    importance=0.6,
    entities=[("Postgres", "tool"), ("Mumbai Region", "place")],
    facts=[("Postgres", "available regions", "Mumbai, Frankfurt")],
)
m.set_procedure("decisions", "Always cite evidence and pick the option that satisfies "
                             "every hard constraint.", agent="planner")

t1 = m.create_task("Choose database + region for the dashboard", "planner",
                   target="researcher")
t2 = m.create_task("Build the dashboard backend", "planner", depends_on=[t1],
                   target="coder")
print("tasks created:", t1, t2)

hr("coordination - the coder cannot jump the queue")
print("coder claims early  ->", m.claim_task("coder"))   # None: blocked by DEPENDS_ON

hr("researcher claims, recalls, decides")
task = m.claim_task("researcher")
print("researcher got:", task["title"])
q = "which database should the Q3 Dashboard use?"
rec = m.recall(q, hops=2)
print(f"seeds found in question: {rec.seeds}")
print(rec.text())
print("\nNote: 'data residency' is about Acme - the question never mentions Acme. "
      "The graph walked Dashboard -> Acme.")

cited = [i for i in rec.items
         if i.kind in ("fact", "procedure") and
         any(w in i.text for w in ("data residency", "available regions", "decisions"))]
did = m.record_decision(
    "researcher", "Use Postgres in the Mumbai region (India-only residency).",
    based_on=cited, task_id=task["id"])
print("verifier ->", m.review_decision(did, "verifier", True,
      "Mumbai satisfies the India-only residency fact"))
m.complete_task(task["id"], "researcher", "Postgres @ Mumbai")

hr("coder now unblocked, receives upstream result")
nxt = m.claim_task("coder")
print("coder got:", nxt["title"], "| upstream:", nxt["upstream"])

hr("provenance of the decision")
print(m.explain(did))

# ---------------------------------------------------------------- SESSION 2
hr("SESSION 2 - a brand-new session, same memory")
gm2 = GraphMemory(HOST, PORT)
m2 = gm2.scope("acme-corp", "priya")
print("graph survived restart:", m2.stats())

m2.remember("planner", "Acme's compliance team has relaxed the rule: EU data centres "
            "are now allowed.", importance=0.9,
            entities=[("Acme", "org")],
            facts=[("Acme", "data residency", "India or EU allowed")])

hr("graph flags decisions whose evidence changed")
for d in m2.stale_decisions():
    print("STALE ->", d["summary"])
print()
print(m2.explain(did))

path = write_trace(m2.why(did), "trace.html")
print(f"\nOpen {path} in a browser for the visual trace.")
print("Or open http://localhost:3000 (FalkorDB Browser), pick graph "
      "'acme-corp__priya', and run:  MATCH (n)-[r]->(m) RETURN n,r,m")

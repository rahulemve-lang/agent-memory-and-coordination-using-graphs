"""
graphmem.py - agent memory + coordination where the graph IS the product.

Everything an agent remembers, plans and decides lives as nodes/edges in FalkorDB:

  (Agent)-[:LOGGED]->(Episode)-[:MENTIONS]->(Entity)-[:RELATED_TO {kind}]-(Entity)
  (Fact)-[:ABOUT]->(Entity)          (Fact)-[:DERIVED_FROM]->(Episode)
  (Fact)-[:SUPERSEDES]->(Fact)       -- old values are kept, never overwritten
  (Agent)-[:CREATED]->(Task)-[:DEPENDS_ON]->(Task)   (Task)-[:HANDED_TO]->(Agent)
  (Agent)-[:MADE]->(Decision)-[:BASED_ON]->(Fact|Episode|Procedure)
  (Decision)-[:FOR]->(Task)

What only a graph gives you here:
  * recall()  = multi-hop traversal from entities in the question (no keyword match)
  * why()     = walks Decision -> evidence -> source episodes -> superseded versions,
                and flags decisions whose evidence has since changed (stale)
  * claim_task() = one atomic Cypher query over the dependency graph
  * isolation = one FalkorDB graph per (tenant, user)

Needs a running FalkorDB:  docker compose up -d
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

__all__ = ["GraphMemory", "Memory", "Item", "Recall", "VersionConflict",
           "Extraction", "HeuristicExtractor", "LLMExtractor", "anthropic_call"]


class VersionConflict(Exception):
    """The record changed since you read it (or already exists). Re-read and retry."""


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


_NAME_OK = re.compile(r"^[A-Za-z0-9_-]+$")
_LABEL = {"fact": "Fact", "episode": "Episode", "procedure": "Procedure"}
_INDEXES = [("Entity", "name"), ("Fact", "id"), ("Fact", "key"), ("Episode", "id"),
            ("Procedure", "id"), ("Procedure", "key"), ("Decision", "id"),
            ("Task", "id"), ("Task", "status"), ("Agent", "name")]


# ----------------------------------------------------------------- data classes
@dataclass
class Item:
    kind: str                     # fact | episode | procedure
    id: str
    text: str
    version: int = 0
    hops: int = 0                 # graph distance from the entities in the question
    score: float = 0.0
    props: dict = field(default_factory=dict)

    @property
    def tag(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass
class Recall:
    seeds: list
    items: list                   # every Item retrieved (cite these in record_decision)

    def of(self, kind: str) -> list:
        return [i for i in self.items if i.kind == kind]

    def text(self) -> str:
        out = []
        if self.of("procedure"):
            out.append("## Procedures")
            out += [f"- [{i.tag}] {i.text}" for i in self.of("procedure")]
        if self.of("fact"):
            out.append("## Facts")
            out += [f"- [{i.tag}] {i.text}  (hops={i.hops})" for i in self.of("fact")]
        if self.of("episode"):
            out.append("## Past events")
            out += [f"- [{i.tag}] {i.text}  (hops={i.hops})" for i in self.of("episode")]
        return "\n".join(out)


@dataclass
class Extraction:
    entities: list = field(default_factory=list)    # [(name, type)]
    relations: list = field(default_factory=list)   # [(src, kind, dst)]
    facts: list = field(default_factory=list)       # [(subject, predicate, value)]


# ------------------------------------------------------------------- extractors
class HeuristicExtractor:
    """Zero-dependency fallback: capitalised phrases become entities and are linked
    as 'co_mentioned'. Weak on purpose - use LLMExtractor for real quality."""
    _SKIP = {"The", "A", "An", "I", "We", "He", "She", "It", "This", "That", "They",
             "Then", "When", "If", "And", "But", "So", "Our", "My", "Your", "In", "On"}

    def extract(self, text: str) -> Extraction:
        names, seen = [], set()
        for m in re.findall(r"\b[A-Z][\w-]*(?:\s+[A-Z0-9][\w-]*)*", text):
            words = [w for w in m.split() if w not in self._SKIP]
            n = " ".join(words)
            if n and _norm(n) not in seen:
                seen.add(_norm(n)); names.append(n)
        names = names[:8]
        rels = [(a, "co_mentioned", b) for i, a in enumerate(names) for b in names[i + 1:]]
        return Extraction([(n, "") for n in names], rels, [])


_PROMPT = """Extract a knowledge graph from the text. Reply with ONLY JSON:
{{"entities":[{{"name":"...","type":"person|org|project|tool|place|concept"}}],
 "relations":[{{"src":"entity name","kind":"short_snake_case_verb","dst":"entity name"}}],
 "facts":[{{"subject":"entity name","predicate":"attribute","value":"current value"}}]}}
Only include facts that state a current value of something (a budget, a deadline, a choice, a constraint).
Text: {text}"""


class LLMExtractor:
    """call(prompt) -> str can be any LLM (see anthropic_call)."""

    def __init__(self, call: Callable[[str], str]):
        self.call = call

    def extract(self, text: str) -> Extraction:
        raw = self.call(_PROMPT.format(text=text))
        a, b = raw.find("{"), raw.rfind("}")
        try:
            d = json.loads(raw[a:b + 1])
        except Exception:
            return HeuristicExtractor().extract(text)
        ents = [(e["name"], e.get("type", "")) for e in d.get("entities", []) if e.get("name")]
        rels = [(r["src"], r.get("kind", "related"), r["dst"]) for r in d.get("relations", [])
                if r.get("src") and r.get("dst")]
        facts = [(f["subject"], f["predicate"], str(f["value"])) for f in d.get("facts", [])
                 if f.get("subject") and f.get("predicate") and "value" in f]
        return Extraction(ents, rels, facts)


def anthropic_call(model: Optional[str] = None, max_tokens: int = 800) -> Callable[[str], str]:
    import os
    import anthropic
    client = anthropic.Anthropic()
    model = model or os.environ.get("EXTRACTOR_MODEL", "claude-haiku-5-5")

    def call(prompt: str) -> str:
        r = client.messages.create(model=model, max_tokens=max_tokens,
                                   messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if b.type == "text")
    return call


# --------------------------------------------------------------------- the store
class GraphMemory:
    def __init__(self, host: str = "localhost", port: int = 6379,
                 password: Optional[str] = None, extractor=None):
        from falkordb import FalkorDB
        self.db = FalkorDB(host=host, port=port, password=password)
        self.extractor = extractor or HeuristicExtractor()
        self._indexed: set = set()

    def scope(self, tenant: str, user: str) -> "Memory":
        return Memory(self, tenant, user)


class Memory:
    """Bound to ONE (tenant, user) = ONE FalkorDB graph. No API crosses graphs."""

    def __init__(self, gm: GraphMemory, tenant: str, user: str):
        for v in (tenant, user):
            if not _NAME_OK.match(v or "") or "__" in v:
                raise ValueError("tenant/user must match [A-Za-z0-9_-]+ and not contain '__'")
        self.gm = gm
        self.name = f"{tenant}__{user}"
        self.g = gm.db.select_graph(self.name)
        self._ensure_indexes()

    # ------------------------------------------------------------------ plumbing
    def _q(self, cypher: str, **params):
        return self.g.query(cypher, params).result_set

    def _ensure_indexes(self):
        if self.name in self.gm._indexed:
            return
        for label, prop in _INDEXES:
            try:
                self.g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception:
                pass  # already exists
        self.gm._indexed.add(self.name)

    @staticmethod
    def _item(kind: str, node, hops: int = 0) -> Item:
        p = node.properties
        if kind == "fact":
            text = f"{p['subject']} / {p['predicate']}: {p['value']}"
        elif kind == "procedure":
            text = f"{p['subject']}: {p['value']}"
        else:
            text = p["text"]
        return Item(kind, p["id"], text, int(p.get("version", 0)), hops, 0.0, dict(p))

    def ensure_agent(self, name: str):
        self._q("MERGE (a:Agent {name:$n}) ON CREATE SET a.ts=$ts", n=name, ts=time.time())

    def _entity(self, name: str, etype: str = "") -> str:
        k = _norm(name)
        self._q("MERGE (e:Entity {name:$k}) ON CREATE SET e.display=$d, e.type=$t, e.ts=$ts",
                k=k, d=name.strip(), t=etype, ts=time.time())
        return k

    # ---------------------------------------------------------------- remembering
    def remember(self, agent: str, text: str, importance: float = 0.5,
                 entities: Optional[list] = None, relations: Optional[list] = None,
                 facts: Optional[list] = None) -> str:
        """Store an episode AND grow the graph around it. If you pass no structured
        data, the extractor (LLM or heuristic) derives entities/relations/facts."""
        if entities is None and relations is None and facts is None:
            ex = self.gm.extractor.extract(text)
            entities, relations, facts = ex.entities, ex.relations, ex.facts
        entities, relations, facts = entities or [], relations or [], facts or []
        self.ensure_agent(agent)
        ep = _id()
        self._q("MATCH (a:Agent {name:$agent}) "
                "CREATE (ep:Episode {id:$id, text:$text, importance:$imp, ts:$ts, agent:$agent}) "
                "CREATE (a)-[:LOGGED]->(ep)",
                agent=agent, id=ep, text=text, imp=float(importance), ts=time.time())

        def mention(name, etype=""):
            k = self._entity(name, etype)
            self._q("MATCH (ep:Episode {id:$ep}), (e:Entity {name:$k}) MERGE (ep)-[:MENTIONS]->(e)",
                    ep=ep, k=k)
            return k

        for e in entities:
            name, etype = (e if isinstance(e, (tuple, list)) else (e, ""))
            mention(name, etype)
        for src, kind, dst in relations:
            a, b = mention(src), mention(dst)
            self._q("MATCH (a:Entity {name:$a}), (b:Entity {name:$b}) "
                    "MERGE (a)-[r:RELATED_TO {kind:$kind}]->(b) ON CREATE SET r.ts=$ts",
                    a=a, b=b, kind=_norm(kind).replace(" ", "_"), ts=time.time())
        for subject, predicate, value in facts:
            mention(subject)
            self.set_fact(subject, predicate, value, agent=agent, source_episode=ep)
        return ep

    # ------------------------------------------- versioned facts / procedures
    def _put_once(self, label, key, subject, predicate, value, agent, expected, source):
        nid, ts = _id(), time.time()
        props = ("id:$id, key:$key, subject:$subject, predicate:$predicate, value:$value, "
                 "agent:$agent, ts:$ts, current:true")
        p = dict(id=nid, key=key, subject=subject, predicate=predicate, value=value,
                 agent=agent or "", ts=ts)
        if expected == 0:   # create only if no current version exists
            q = ("OPTIONAL MATCH (x:__L__ {key:$key, current:true}) WITH x WHERE x IS NULL "
                 "CREATE (n:__L__ {__P__, version:1}) RETURN n.version")
        else:               # compare-and-swap: only supersede the exact version we read
            q = ("MATCH (old:__L__ {key:$key, current:true, version:$exp}) "
                 "SET old.current = false "
                 "CREATE (n:__L__ {__P__, version:$nv}) "
                 "CREATE (n)-[:SUPERSEDES]->(old) RETURN n.version")
            p.update(exp=expected, nv=expected + 1)
        q = q.replace("__L__", label).replace("__P__", props)
        rows = self._q(q, **p)
        if not rows:
            raise VersionConflict(f"{label} '{key}' is not at version {expected}")
        if label == "Fact":
            k = self._entity(subject)
            self._q("MATCH (f:Fact {id:$id}), (e:Entity {name:$k}) CREATE (f)-[:ABOUT]->(e)",
                    id=nid, k=k)
        if source:
            self._q("MATCH (n {id:$id}), (ep:Episode {id:$ep}) CREATE (n)-[:DERIVED_FROM]->(ep)",
                    id=nid, ep=source)
        return rows[0][0]

    def _current(self, label, key) -> Optional[Item]:
        rows = self._q(f"MATCH (n:{label} {{key:$key, current:true}}) RETURN n", key=key)
        return self._item(label.lower(), rows[0][0]) if rows else None

    def _put(self, label, key, subject, predicate, value, agent, expected, source):
        if expected is not None:
            return self._put_once(label, key, subject, predicate, value, agent, expected, source)
        for _ in range(10):  # no version given: read-modify-write with retry
            cur = self._current(label, key)
            if cur and cur.props["value"] == value:
                return cur.version  # idempotent: nothing changed
            try:
                return self._put_once(label, key, subject, predicate, value, agent,
                                      cur.version if cur else 0, source)
            except VersionConflict:
                continue
        raise VersionConflict(f"too much contention on {label} '{key}'")

    def set_fact(self, subject: str, predicate: str, value: str, agent: Optional[str] = None,
                 expected_version: Optional[int] = None,
                 source_episode: Optional[str] = None) -> int:
        """Change a fact. The old value stays in the graph, linked by SUPERSEDES."""
        key = f"{_norm(subject)}::{_norm(predicate)}"
        return self._put("Fact", key, subject, predicate, str(value), agent,
                         expected_version, source_episode)

    def get_fact(self, subject: str, predicate: str) -> Optional[Item]:
        return self._current("Fact", f"{_norm(subject)}::{_norm(predicate)}")

    def fact_history(self, subject: str, predicate: str) -> list:
        key = f"{_norm(subject)}::{_norm(predicate)}"
        rows = self._q("MATCH (n:Fact {key:$key}) RETURN n ORDER BY n.version", key=key)
        return [self._item("fact", r[0]) for r in rows]

    def set_procedure(self, name: str, instructions: str, agent: Optional[str] = None,
                      expected_version: Optional[int] = None) -> int:
        return self._put("Procedure", _norm(name), name, "procedure", instructions, agent,
                         expected_version, None)

    def procedures(self) -> list:
        rows = self._q("MATCH (n:Procedure {current:true}) RETURN n ORDER BY n.subject")
        return [self._item("procedure", r[0]) for r in rows]

    # ----------------------------------------------------------------- recall
    def recall(self, question: str, hops: int = 2, limit: int = 8,
               seeds: Optional[list] = None) -> Recall:
        """Find entities named in the question, then WALK the graph `hops` steps and
        return facts/episodes attached to everything reached. A fact about something
        two hops away is found even if the question never mentions it."""
        hops = max(0, min(int(hops), 4))
        if seeds is None:
            low = _norm(question)
            seeds = [r[0] for r in self._q("MATCH (e:Entity) RETURN e.name")
                     if re.search(r"(?<!\w)" + re.escape(r[0]) + r"(?!\w)", low)]
        else:
            seeds = [_norm(s) for s in seeds]
        dist = {s: 0 for s in seeds}
        if seeds and hops > 0:
            q = ("MATCH p=(s:Entity)-[:RELATED_TO*1..__H__]-(e:Entity) WHERE s.name IN $seeds "
                 "RETURN e.name, min(length(p))").replace("__H__", str(hops))
            for name, d in self._q(q, seeds=seeds):
                dist[name] = min(dist.get(name, 99), int(d))
        items: list = list(self.procedures())
        if dist:
            names = list(dist)
            facts = []
            for node, en in self._q("MATCH (f:Fact {current:true})-[:ABOUT]->(e:Entity) "
                                    "WHERE e.name IN $names RETURN f, e.name", names=names):
                it = self._item("fact", node, dist[en])
                it.score = 0.5 ** dist[en]
                facts.append(it)
            eps: dict = {}
            now = time.time()
            for node, en in self._q("MATCH (ep:Episode)-[:MENTIONS]->(e:Entity) "
                                    "WHERE e.name IN $names RETURN ep, e.name", names=names):
                it = self._item("episode", node, dist[en])
                age_days = max(0.0, now - it.props["ts"]) / 86400
                it.score = float(it.props["importance"]) * 0.5 ** dist[en] * 0.5 ** (age_days / 30)
                if it.id not in eps or it.score > eps[it.id].score:
                    eps[it.id] = it
            items += sorted(facts, key=lambda i: -i.score)[:limit]
            items += sorted(eps.values(), key=lambda i: -i.score)[:limit]
        return Recall(seeds, items)

    # --------------------------------------------------------------- decisions
    def record_decision(self, agent: str, summary: str, based_on: Iterable[Item],
                        task_id: Optional[str] = None) -> str:
        """Log a decision and link it to the exact evidence it used."""
        self.ensure_agent(agent)
        did = _id()
        self._q("MATCH (a:Agent {name:$agent}) "
                "CREATE (d:Decision {id:$id, summary:$s, ts:$ts, status:'proposed'}) "
                "CREATE (a)-[:MADE]->(d)",
                agent=agent, id=did, s=summary, ts=time.time())
        for it in based_on:
            self._q(f"MATCH (d:Decision {{id:$d}}), (x:{_LABEL[it.kind]} {{id:$x}}) "
                    "CREATE (d)-[:BASED_ON]->(x)", d=did, x=it.id)
        if task_id:
            self._q("MATCH (d:Decision {id:$d}), (t:Task {id:$t}) CREATE (d)-[:FOR]->(t)",
                    d=did, t=task_id)
        return did

    def review_decision(self, decision_id: str, agent: str, approved: bool,
                        reason: str = "") -> str:
        """A verifier approves/rejects a proposed decision. Stored as a REVIEWED edge
        carrying the verdict and reason, so the review is part of the trace."""
        self.ensure_agent(agent)
        status = "approved" if approved else "rejected"
        rows = self._q("MATCH (a:Agent {name:$agent}), (d:Decision {id:$id}) "
                       "SET d.status = $status "
                       "CREATE (a)-[:REVIEWED {verdict:$status, reason:$reason, ts:$ts}]->(d) "
                       "RETURN d.id", agent=agent, id=decision_id, status=status,
                       reason=reason, ts=time.time())
        if not rows:
            raise KeyError(decision_id)
        return status

    def why(self, decision_id: str) -> dict:
        """Full provenance of a decision. Each piece of evidence is flagged `stale` if
        a newer version of that fact exists now."""
        head = self._q("MATCH (a:Agent)-[:MADE]->(d:Decision {id:$id}) "
                       "RETURN a.name, d.summary, d.ts, d.status", id=decision_id)
        if not head:
            raise KeyError(decision_id)
        agent, summary, ts, status = head[0]
        task = self._q("MATCH (d:Decision {id:$id})-[:FOR]->(t:Task) RETURN t.title", id=decision_id)
        reviews = [{"agent": r[0], "verdict": r[1], "reason": r[2]} for r in self._q(
            "MATCH (a:Agent)-[r:REVIEWED]->(d:Decision {id:$id}) "
            "RETURN a.name, r.verdict, r.reason ORDER BY r.ts", id=decision_id)]
        evidence = []
        for (x,) in self._q("MATCH (d:Decision {id:$id})-[:BASED_ON]->(x) RETURN x", id=decision_id):
            kind = x.labels[0].lower()
            it = self._item(kind, x)
            ev = {"kind": kind, "id": it.id, "text": it.text, "stale": False,
                  "current_text": None, "derived_from": [], "history": []}
            if kind in ("fact", "procedure"):
                key = it.props["key"]
                label = _LABEL[kind]
                cur = self._q(f"MATCH (n:{label} {{key:$key, current:true}}) RETURN n", key=key)
                if cur and cur[0][0].properties["id"] != it.id:
                    ev["stale"] = True
                    ev["current_text"] = self._item(kind, cur[0][0]).text
                ev["history"] = [{"version": h[0].properties["version"],
                                  "text": self._item(kind, h[0]).text}
                                 for h in self._q(f"MATCH (n:{label} {{key:$key}}) "
                                                  "WHERE n.version < $v RETURN n ORDER BY n.version",
                                                  key=key, v=it.version)]
                ev["derived_from"] = [{"id": r[0], "text": r[1]} for r in self._q(
                    "MATCH (n {id:$id})-[:DERIVED_FROM]->(ep:Episode) RETURN ep.id, ep.text",
                    id=it.id)]
            evidence.append(ev)
        return {"id": decision_id, "agent": agent, "summary": summary, "ts": ts,
                "status": status or "proposed", "reviews": reviews, "task": task[0][0] if task else None, "evidence": evidence}

    def explain(self, decision_id: str) -> str:
        w = self.why(decision_id)
        lines = [f"{w['agent']} decided: {w['summary']}  [{w['status']}]"]
        for r in w["reviews"]:
            lines.append(f"  reviewed by {r['agent']}: {r['verdict']} - {r['reason']}")
        if w["task"]:
            lines.append(f"  for task: {w['task']}")
        for e in w["evidence"]:
            flag = f"  !! STALE - now: {e['current_text']}" if e["stale"] else ""
            lines.append(f"  <- {e['kind']}: {e['text']}{flag}")
            for d in e["derived_from"]:
                lines.append(f"       from episode: {d['text']}")
            for h in e["history"]:
                lines.append(f"       replaced v{h['version']}: {h['text']}")
        return "\n".join(lines)

    def stale_decisions(self) -> list:
        """Decisions resting on a fact/procedure that has since been superseded."""
        rows = self._q("MATCH (d:Decision)-[:BASED_ON]->(x) WHERE x.current = false "
                       "RETURN DISTINCT d.id, d.summary")
        return [{"id": r[0], "summary": r[1]} for r in rows]

    # ------------------------------------------------------------------ tasks
    def create_task(self, title: str, from_agent: str, depends_on: Iterable[str] = (),
                    target: str = "", payload: Optional[dict] = None) -> str:
        """target='' means any agent may claim it; otherwise only that agent."""
        deps = list(dict.fromkeys(depends_on))
        if deps:
            n = self._q("MATCH (d:Task) WHERE d.id IN $d RETURN count(d)", d=deps)[0][0]
            if n != len(deps):
                raise ValueError("unknown task in depends_on")
        self.ensure_agent(from_agent)
        tid = _id()
        q = ("MATCH (a:Agent {name:$frm}) "
             "CREATE (t:Task {id:$id, title:$title, status:'pending', target:$target, "
             "payload:$payload, ts:$ts}) CREATE (a)-[:CREATED]->(t)")
        if deps:  # same query -> task never exists without its dependency edges
            q += " WITH t UNWIND $deps AS dep MATCH (d:Task {id:dep}) CREATE (t)-[:DEPENDS_ON]->(d)"
        self._q(q, frm=from_agent, id=tid, title=title, target=target,
                payload=json.dumps(payload or {}), ts=time.time(), deps=deps)
        return tid

    def claim_task(self, agent: str) -> Optional[dict]:
        """Atomically claim the oldest pending task whose dependencies are all done.
        FalkorDB serialises writes per graph, so two agents can never get the same one."""
        self.ensure_agent(agent)
        rows = self._q(
            "MATCH (t:Task {status:'pending'}) WHERE t.target = '' OR t.target = $agent "
            "OPTIONAL MATCH (t)-[:DEPENDS_ON]->(d:Task) WHERE d.status <> 'done' "
            "WITH t, count(d) AS blockers WHERE blockers = 0 "
            "WITH t ORDER BY t.ts LIMIT 1 "
            "SET t.status = 'claimed', t.claimed_by = $agent, t.claimed_at = $ts "
            "WITH t MATCH (a:Agent {name:$agent}) CREATE (t)-[:HANDED_TO]->(a) RETURN t",
            agent=agent, ts=time.time())
        if not rows:
            return None
        p = rows[0][0].properties
        up = self._q("MATCH (t:Task {id:$id})-[:DEPENDS_ON]->(d:Task) RETURN d.title, d.result",
                     id=p["id"])
        return {"id": p["id"], "title": p["title"], "payload": json.loads(p["payload"]),
                "upstream": [{"title": r[0], "result": r[1]} for r in up]}

    def complete_task(self, task_id: str, agent: str, result: str = "") -> None:
        rows = self._q("MATCH (t:Task {id:$id, claimed_by:$agent}) "
                       "SET t.status='done', t.result=$res, t.done_at=$ts RETURN t.id",
                       id=task_id, agent=agent, res=result, ts=time.time())
        if not rows:
            raise ValueError("task not claimed by this agent")

    # ------------------------------------------------------------- housekeeping
    def forget(self, older_than_days: float = 30, max_importance: float = 0.3) -> int:
        """Delete old low-importance episodes - but NEVER ones a decision cites or a
        fact was derived from, so provenance can't silently break."""
        res = self.g.query(
            "MATCH (ep:Episode) WHERE ep.ts < $cut AND ep.importance <= $imp "
            "OPTIONAL MATCH (x)-[:BASED_ON|DERIVED_FROM]->(ep) "
            "WITH ep, count(x) AS n WHERE n = 0 DETACH DELETE ep",
            {"cut": time.time() - older_than_days * 86400, "imp": max_importance})
        return res.nodes_deleted

    def stats(self) -> dict:
        return {r[0]: r[1] for r in self._q("MATCH (n) RETURN labels(n)[0], count(n)")}

    def erase(self) -> None:
        """Delete this user's entire graph."""
        try:
            self.g.delete()
        except Exception:
            pass
        self.gm._indexed.discard(self.name)

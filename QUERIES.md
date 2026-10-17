# Cypher queries the product depends on

Every query below lives in `graphmem.py` and runs against FalkorDB. Parameters are written
`$like_this`. A label like `__L__` is substituted in code with `Fact` or `Procedure` (labels
cannot be parameters in Cypher; the code only ever substitutes those two fixed values).

**Status key**
- **Live** = exercised end to end on a real FalkorDB during the demo run (planner, researcher,
  verifier, coder, stale-decision trace).
- **Written, not yet run live** = in the code and covered by `test_graphmem.py`, but I have not
  run it against a live database.

---

## 1. Writing memory

**Agents and entities (idempotent upserts)** - Live
```cypher
MERGE (a:Agent {name:$n}) ON CREATE SET a.ts=$ts
MERGE (e:Entity {name:$k}) ON CREATE SET e.display=$d, e.type=$t, e.ts=$ts
```
`MERGE` makes repeated mentions of "Acme" resolve to one node. Entity names are lowercased
for matching; the original spelling is kept in `display`.

**An episode, linked to its author and to what it mentions** - Live
```cypher
MATCH (a:Agent {name:$agent})
CREATE (ep:Episode {id:$id, text:$text, importance:$imp, ts:$ts, agent:$agent})
CREATE (a)-[:LOGGED]->(ep)

MATCH (ep:Episode {id:$ep}), (e:Entity {name:$k}) MERGE (ep)-[:MENTIONS]->(e)
```

**Relations between entities** - Live
```cypher
MATCH (a:Entity {name:$a}), (b:Entity {name:$b})
MERGE (a)-[r:RELATED_TO {kind:$kind}]->(b) ON CREATE SET r.ts=$ts
```
One edge type with a `kind` property (instead of a dynamic edge type) avoids building Cypher
from model output. This is what recall traverses.

## 2. Facts that change without losing history

**Create a fact only if none is current** - Live
```cypher
OPTIONAL MATCH (x:__L__ {key:$key, current:true})
WITH x WHERE x IS NULL
CREATE (n:__L__ {id:$id, key:$key, subject:$subject, predicate:$predicate,
                 value:$value, agent:$agent, ts:$ts, current:true, version:1})
RETURN n.version
```
If a current version exists the `WHERE x IS NULL` filter removes the row, nothing is created,
and the caller sees a conflict.

**Compare-and-swap update that keeps the old value** - Live
```cypher
MATCH (old:__L__ {key:$key, current:true, version:$exp})
SET old.current = false
CREATE (n:__L__ {id:$id, key:$key, subject:$subject, predicate:$predicate,
                 value:$value, agent:$agent, ts:$ts, current:true, version:$nv})
CREATE (n)-[:SUPERSEDES]->(old)
RETURN n.version
```
This is one statement, so it is atomic. It matches only if the fact is still at the version
the caller read. A concurrent change makes it match nothing, which becomes `VersionConflict`.
The old value stays in the graph, linked by `SUPERSEDES`. This chain is what makes
stale-decision detection possible.

**Attach a fact to its subject and its source episode** - Live
```cypher
MATCH (f:Fact {id:$id}), (e:Entity {name:$k}) CREATE (f)-[:ABOUT]->(e)
MATCH (n {id:$id}), (ep:Episode {id:$ep}) CREATE (n)-[:DERIVED_FROM]->(ep)
```

**Current value / history of a fact** - Live (current) / Written, not yet run live (history)
```cypher
MATCH (n:__L__ {key:$key, current:true}) RETURN n
MATCH (n:Fact {key:$key}) RETURN n ORDER BY n.version
```

## 3. Recall: multi-hop retrieval (the graph at work)

**Step 1: find entities named in the question** - Live
```cypher
MATCH (e:Entity) RETURN e.name
```
(Matched against the question text in Python with word boundaries.)

**Step 2: walk the graph up to N hops from those entities** - Live
```cypher
MATCH p=(s:Entity)-[:RELATED_TO*1..2]-(e:Entity) WHERE s.name IN $seeds
RETURN e.name, min(length(p))
```
Undirected, bounded variable-length traversal; the shortest distance to each reached entity
becomes its relevance weight. A fact about something two hops away is found even though the
question never names it. `test_multihop_recall_needs_the_graph` checks this: the fact appears
with `hops=2` and not with `hops=1`.

**Step 3: collect current facts and episodes attached to everything reached** - Live
```cypher
MATCH (f:Fact {current:true})-[:ABOUT]->(e:Entity) WHERE e.name IN $names RETURN f, e.name
MATCH (ep:Episode)-[:MENTIONS]->(e:Entity)        WHERE e.name IN $names RETURN ep, e.name
```

**Standing procedures** - Live (query runs on every recall; no procedures have been stored yet)
```cypher
MATCH (n:Procedure {current:true}) RETURN n ORDER BY n.subject
```

## 4. Decisions and their provenance

**Record a decision and link it to the exact evidence it cites** - Live
```cypher
MATCH (a:Agent {name:$agent})
CREATE (d:Decision {id:$id, summary:$s, ts:$ts, status:'proposed'})
CREATE (a)-[:MADE]->(d)

MATCH (d:Decision {id:$d}), (x:Fact {id:$x}) CREATE (d)-[:BASED_ON]->(x)   // or Episode / Procedure
MATCH (d:Decision {id:$d}), (t:Task {id:$t}) CREATE (d)-[:FOR]->(t)
```

**An independent verifier's verdict, stored on the graph** - Live
```cypher
MATCH (a:Agent {name:$agent}), (d:Decision {id:$id})
SET d.status = $status
CREATE (a)-[:REVIEWED {verdict:$status, reason:$reason, ts:$ts}]->(d)
RETURN d.id
```

**Why did the agent decide this? (provenance walk)** - Live
```cypher
MATCH (a:Agent)-[:MADE]->(d:Decision {id:$id}) RETURN a.name, d.summary, d.ts, d.status
MATCH (a:Agent)-[r:REVIEWED]->(d:Decision {id:$id}) RETURN a.name, r.verdict, r.reason ORDER BY r.ts
MATCH (d:Decision {id:$id})-[:BASED_ON]->(x) RETURN x
MATCH (n {id:$id})-[:DERIVED_FROM]->(ep:Episode) RETURN ep.id, ep.text
MATCH (n:__L__ {key:$key}) WHERE n.version < $v RETURN n ORDER BY n.version
```
For each piece of evidence the code also checks whether a different version is now current;
if so the evidence is flagged **stale** and the newer value is shown.

**Which decisions now rest on outdated evidence?** - Live
```cypher
MATCH (d:Decision)-[:BASED_ON]->(x) WHERE x.current = false
RETURN DISTINCT d.id, d.summary
```
Works because replacing a fact sets the old node's `current` to false while the decision still
points at it.

## 5. Coordination: a task graph agents claim from

**Create a task with its dependencies in a single statement** - Live
```cypher
MATCH (a:Agent {name:$frm})
CREATE (t:Task {id:$id, title:$title, status:'pending', target:$target, payload:$payload, ts:$ts})
CREATE (a)-[:CREATED]->(t)
WITH t UNWIND $deps AS dep MATCH (d:Task {id:dep}) CREATE (t)-[:DEPENDS_ON]->(d)
```
One statement means a task never exists without its dependency edges, so no agent can claim it
too early.

**Atomically claim the oldest task whose dependencies are all done** - Live
```cypher
MATCH (t:Task {status:'pending'}) WHERE t.target = '' OR t.target = $agent
OPTIONAL MATCH (t)-[:DEPENDS_ON]->(d:Task) WHERE d.status <> 'done'
WITH t, count(d) AS blockers WHERE blockers = 0
WITH t ORDER BY t.ts LIMIT 1
SET t.status = 'claimed', t.claimed_by = $agent, t.claimed_at = $ts
WITH t MATCH (a:Agent {name:$agent}) CREATE (t)-[:HANDED_TO]->(a)
RETURN t
```
Find-and-claim in one statement; FalkorDB runs write queries on a graph one at a time, so two
agents cannot get the same task. `test_tasks_respect_dependencies_and_claim_once` races four
workers on this.

**Upstream results for the claiming agent; completing a task** - Live
```cypher
MATCH (t:Task {id:$id})-[:DEPENDS_ON]->(d:Task) RETURN d.title, d.result
MATCH (t:Task {id:$id, claimed_by:$agent}) SET t.status='done', t.result=$res, t.done_at=$ts RETURN t.id
```

## 6. Housekeeping

**Forget old, low-importance episodes, but never ones a decision or fact depends on** - Written, not yet run live
```cypher
MATCH (ep:Episode) WHERE ep.ts < $cut AND ep.importance <= $imp
OPTIONAL MATCH (x)-[:BASED_ON|DERIVED_FROM]->(ep)
WITH ep, count(x) AS n WHERE n = 0 DETACH DELETE ep
```
Protects provenance: cleanup can't silently break a decision's trace.

**Graph statistics; wipe a user's graph; indexes** - Statistics and wipe: written, not yet run live. Indexes: live (created at startup).
```cypher
MATCH (n) RETURN labels(n)[0], count(n)
CREATE INDEX FOR (n:Label) ON (n.property)     // Entity.name, Fact.id/key, Episode.id, Task.id/status, ...
```
Isolation: each (tenant, user) pair gets its own FalkorDB graph (`<tenant>__<user>`), and erasing
a user means deleting that graph.

# graphmem: agents that remember in a graph and can show why they decided

**Hackathon:** Graph Hacks: Context for AI Agents (FalkorDB)
**Track entered:** Agent Memory and Coordination

A small team of AI agents (planner, researcher, verifier, coder) shares one memory that lives
entirely in FalkorDB. Every decision an agent makes is linked to the exact facts and events it
used. When a fact later changes, the graph tells you which decisions are now built on outdated
evidence, and shows what changed.

## Why the graph is not decoration

Take FalkorDB out and four things stop working, because each is a graph query:

| Feature | What the graph does |
|---|---|
| **Recall** | Walks `RELATED_TO` edges up to 2 hops from the entities in a question. A fact about something the question never names (Acme's data-residency rule, found from "Q3 Dashboard") is still found. |
| **Decision provenance** | `Decision -[:BASED_ON]-> Fact -[:DERIVED_FROM]-> Episode`, plus `SUPERSEDES` chains for older values. "Why did the agent decide this?" is a traversal. |
| **Stale-decision detection** | Replacing a fact marks the old node `current=false`; a one-line query finds every decision still pointing at it. |
| **Coordination** | Tasks form a `DEPENDS_ON` graph. An agent claims the oldest task whose dependencies are all done in a single atomic query. |

The queries are listed with explanations in [`QUERIES.md`](QUERIES.md).

## Architecture

```
                 n8n workflows (4)                graphmem HTTP API            FalkorDB
   Planner (Gemini) ──┐                                                     ┌─ graph per
   Researcher (Gemini)─┼── HTTP Request nodes ──▶  server.py  ──▶ graphmem.py ─▶ tenant+user
   Verifier (Groq) ────┤        (JSON)             (stdlib)       (Cypher)   └──────────────
   Coder (Gemini) ─────┘
```

- **n8n** orchestrates and calls the LLMs. It never talks to the database directly.
- **`server.py`** is a small dependency-free HTTP API (every endpoint: POST + JSON).
- **`graphmem.py`** holds all graph logic and Cypher.
- **Isolation:** one FalkorDB graph per `(tenant, user)`; no API call can cross graphs.
- Memory calls are plain HTTP nodes (not free-roaming tool-calling agents) so the workflow, not
  the model, decides which recalled evidence gets cited. That keeps the trace trustworthy.

## Graph data model

```
(Agent)-[:LOGGED]->(Episode)-[:MENTIONS]->(Entity)-[:RELATED_TO {kind}]-(Entity)
(Fact)-[:ABOUT]->(Entity)            (Fact)-[:DERIVED_FROM]->(Episode)
(Fact)-[:SUPERSEDES]->(Fact)         old values are kept, never overwritten
(Agent)-[:CREATED]->(Task)-[:DEPENDS_ON]->(Task)      (Task)-[:HANDED_TO]->(Agent)
(Agent)-[:MADE]->(Decision)-[:BASED_ON]->(Fact | Episode | Procedure)
(Agent)-[:REVIEWED {verdict, reason}]->(Decision)     (Decision)-[:FOR]->(Task)
```

| Node | Key properties |
|---|---|
| `Entity` | `name` (normalised), `display`, `type` |
| `Episode` | `id`, `text`, `importance`, `ts`, `agent` |
| `Fact` / `Procedure` | `id`, `key`, `subject`, `predicate`, `value`, `version`, `current` |
| `Decision` | `id`, `summary`, `status` (`proposed` / `approved` / `rejected`) |
| `Task` | `id`, `title`, `status` (`pending` / `claimed` / `done`), `target`, `result` |

## Setup (everything runs locally with Docker)

Requirements: Docker Desktop, Python 3 (only for the helper scripts), a free Gemini API key and
a free Groq API key.

```bash
cp .env.example .env     # fill in GRAPHMEM_API_KEY, N8N_ENCRYPTION_KEY, GEMINI_API_KEY, GROQ_API_KEY
docker compose up -d --build
```

This starts FalkorDB (`:6379`, graph browser on `:3000`), the graphmem API (`:8080`) and n8n
(`:5678`). On first start the `n8n-init` service creates the credentials and imports the four
workflows in `n8n/workflows/`. Open http://localhost:5678 and create the owner account.

**If an LLM node reports "model does not exist"**, provider model names change. Open that node's
chat-model sub-node and choose a current model from the dropdown (use a large model for the
verifier). Full workflow details: [`N8N_GUIDE.md`](N8N_GUIDE.md).

## Run the demo

```bash
# 1. load the evidence and a fresh pair of tasks (set GRAPHMEM_API_KEY in your shell first)
python seed.py --tasks
```
2. In n8n, run workflow **2 (Researcher + Verifier)** once: it claims the task, recalls evidence
   through the graph, proposes a decision, has an independent model verify it, and records both.
3. Run workflow **3 (Coder)**: it can only claim its task once the researcher's task is done.
4. Change a fact and see the consequences:
```bash
python demo_stale.py     # changes Acme's data-residency rule, lists stale decisions, writes trace.html
```
Open `trace.html` for the visual decision trace, or open http://localhost:3000, choose graph
`demo__priya` and run `MATCH (n)-[r]->(m) RETURN n,r,m`.

`python test_graphmem.py` runs integration tests against a running FalkorDB (isolation,
optimistic locking, multi-hop recall, concurrent task claiming, provenance, forgetting).

## Models

Planner, researcher and coder run on Google Gemini (a Flash-class model); the verifier and the
memory extractor run on a large Groq-hosted open model. The verifier is deliberately a
different model family from the researcher so it does not share the proposer's blind spots.
Prompts are in [`prompts/`](prompts/). All model choices are editable in n8n.

## Use of AI coding assistants (disclosure)

This project was built with the help of **Claude (Anthropic)**, which was used to write and
revise the Python code (`graphmem.py`, `server.py`, the helper scripts), the Cypher queries, the
agent prompts and the documentation, and to help debug the n8n setup. I ran, tested and
modified the system on my own machine and I can explain the graph model, the queries and the
design decisions.

## Limitations (honest list)

- **Detects stale decisions but does not re-decide them yet.** The graph flags and explains the
  problem; automatically re-running the researcher on changed facts is future work.
- **Small scale.** Demonstrated on a graph of a few dozen nodes. I have not load-tested it, so I
  make no performance claims.
- **Free-tier LLMs rate-limit** and model names change; the workflows retry but a demo can still
  hit limits.
- **Recall matches entity names exactly** (case-insensitive). A question has to contain an
  entity's full name; fuzzy matching is future work.
- **The `forget` cleanup query and some helper queries are written and unit-tested but were not
  part of the demo run** (see status in `QUERIES.md`).

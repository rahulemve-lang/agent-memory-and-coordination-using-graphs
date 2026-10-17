# Building the agents in n8n

n8n runs the loop and calls the LLMs. **The graph does the remembering, routing and
tracing.** n8n never touches FalkorDB directly; it talks to the `graphmem` API.

## Setup
```bash
cp .env.example .env          # set GRAPHMEM_API_KEY, N8N_ENCRYPTION_KEY, GEMINI_API_KEY, GROQ_API_KEY
docker compose up -d --build  # falkordb :6379/:3000, graphmem :8080, n8n :5678
```
On first start, the `n8n-init` service creates the three credentials (graphmem header auth,
Gemini, Groq) from your `.env` and imports the four workflows in `n8n/workflows/`. Nothing to
wire by hand. It skips itself on later starts, so it won't switch off workflows you activated;
set `N8N_RESEED=1` in `.env` and re-run `docker compose up` to re-import from the files.

Open http://localhost:5678 (create the owner account on first visit). Inside n8n the API address
is **`http://graphmem:8080`** (not localhost, since n8n runs in its own container). Every request
body carries `"tenant": "demo", "user": "priya"` (these pick the isolated graph).

Workflows in `n8n/workflows/`:

| File | Trigger | LLM | Job |
|---|---|---|---|
| `1_planner.json` | Manual (edit `goal` in the **Trigger** node) | Gemini | goal -> tasks with dependencies |
| `2_researcher_verifier.json` | Schedule, 20 s | Gemini + Groq verifier | claim -> decide -> verify, one retry, else BLOCKED |
| `3_coder.json` | Schedule, 20 s | Gemini | claim -> build -> extract memory -> complete |
| `4_memory_extraction.json` | Called by 3 (sub-workflow) | Groq | text -> entities/relations/facts -> `/remember` |

Activate 2 and 3 (toggle, top right) after a manual test run. 4 is a sub-workflow and needs no
activation; the coder calls it by ID.

## One rule that matters: use plain nodes, not free-roaming agents
Use **HTTP Request** nodes for every memory call and **Basic LLM Chain** nodes only for
thinking. Do NOT hand the model a "recall" tool and let it call whenever it likes.
The trace is only trustworthy if *your workflow* decides which recalled items get cited,
and tool-calling agents make that unpredictable.

LLM chat-model sub-nodes in the shipped workflows: Planner, Researcher, Coder = Google Gemini
(`gemini-2.5-flash`); Verifier and Extractor = Groq (`llama-3.3-70b-versatile`). Retry On Fail is
already on for every LLM node. To swap a provider, replace the chat-model sub-node and keep the
prompt.

## Workflow 1 - Planner (trigger: Chat or Manual with a `goal`)
1. **HTTP** `POST /recall` `{tenant,user, question: "{{ $json.goal }}"}`
2. **Basic LLM Chain** (Gemini). Prompt:
   ```
   Goal: {{ $('Trigger').item.json.goal }}
   Known context:
   {{ $json.context }}
   Break the goal into 2-5 tasks. Reply ONLY with JSON:
   {"tasks":[{"title":"...","target":"researcher|coder","after":[indexes of EARLIER tasks]}]}
   ```
3. **Code** node: parse (`JSON.parse($json.text.replace(/```json|```/g,''))`)
4. **HTTP** `POST /task/create_many`
   `{tenant,user, from_agent:"planner", tasks: {{ $json.tasks }}}`
   (`after` indexes are turned into DEPENDS_ON edges for you.)

## Workflow 2 - Researcher + Verifier (trigger: Schedule, every 15-30 s)
1. **HTTP** `POST /task/claim` `{tenant,user, agent:"researcher"}`
2. **IF** `{{ $json.task }}` is not empty (else stop)
3. **HTTP** `POST /recall` `{tenant,user, question: "{{ $json.task.title }}", hops:2}`
4. **Basic LLM Chain** (Gemini), researcher. Prompt:
   ```
   Task: {{ title }}      Upstream results: {{ upstream }}
   Evidence (cite by tag):
   {{ $json.context }}
   Propose ONE decision. Reply ONLY with JSON:
   {"summary":"...","cited":["fact:...","procedure:..."]}   <- copy tags exactly
   ```
5. **Code**: parse JSON
6. **HTTP** `POST /decide`
   `{tenant,user, agent:"researcher", summary, based_on: cited, task_id}` -> `decision_id`
7. **Basic LLM Chain** (Groq), verifier. Prompt:
   ```
   You are a strict verifier. Evidence:
   {{ recall context }}
   Proposed decision: {{ summary }}
   Does it contradict ANY fact or procedure above? Reply ONLY with JSON:
   {"approved":true|false,"reason":"cite the fact tag you relied on"}
   ```
8. **HTTP** `POST /review` `{tenant,user, decision_id, agent:"verifier", approved, reason}`
9. **IF approved** -> **HTTP** `POST /task/complete` `{task_id, agent:"researcher", result: summary}`
   **else** loop back to step 4 once or twice with the verifier's `reason` appended; if it
   still fails, complete the task with `result: "BLOCKED: <reason>"` so it is visible.

## Workflow 3 - Coder (trigger: Schedule)
1. **HTTP** `POST /task/claim` `{agent:"coder"}` (it only returns once its dependencies are
   done, and includes `upstream` results)
2. **IF** task exists -> **Basic LLM Chain** (Gemini) with title + upstream
3. **HTTP** `POST /remember` `{agent:"coder", text: <what it built>, importance:0.6}`
4. **HTTP** `POST /task/complete`

## Workflow 4 - Memory extraction (shipped as 4_memory_extraction.json; the coder calls it)
The API's built-in extractor is weak, so extract in n8n with an LLM and pass structure in:
1. **Basic LLM Chain** (small Groq model):
   ```
   Extract a knowledge graph from the text. Reply ONLY with JSON:
   {"entities":[{"name":"","type":""}],
    "relations":[{"src":"","kind":"snake_case_verb","dst":""}],
    "facts":[{"subject":"","predicate":"","value":""}]}
   Only put CURRENT values of things (budget, deadline, choice, constraint) in facts.
   Text: {{ text }}
   ```
2. **Code**: map to the API shape: `entities: [[name,type],...]`,
   `relations: [[src,kind,dst],...]`, `facts: [[subject,predicate,value],...]`
3. **HTTP** `POST /remember` with those arrays.

## The demo moment
Change a fact with `POST /fact/set` (e.g. Acme residency -> "India or EU allowed"), then:
- `POST /stale` lists decisions now resting on outdated evidence
- `POST /why {decision_id}` returns the full trace; `viz.py`'s `trace_html()` renders it
- Graph view: http://localhost:3000 -> graph `demo__priya` ->
  `MATCH (n)-[r]->(m) RETURN n,r,m`

## Gotchas
- n8n node and field names shift between versions; treat the labels above as a map, not gospel.
- Free LLM tiers rate-limit. Keep the Schedule trigger slow (15-30 s) and keep the retry settings.
- `cited` tags must be copied exactly. If a model garbles one, `/decide` returns 400 - route
  that to a retry rather than letting it pass silently.

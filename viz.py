"""viz.py - render Memory.why(decision_id) as a standalone HTML page (inline SVG, no deps).

    from viz import write_trace
    write_trace(mem.why(decision_id), "trace.html")

Layout (left to right):  agent + verifier -> decision -> evidence -> sources
  * full text is shown (wrapped), never cut off
  * a stale fact shows its CURRENT replacement in its own box
  * identical source episodes are drawn once, with several edges into them
"""
import html
import textwrap

PAD, HEAD, LH, GAP = 10, 18, 15, 18
CHAR_PX = 6.6                      # conservative average glyph width at 12px
X0, W0 = 20, 190                   # agent / verifier
X1, W1 = 300, 260                  # decision
X2, W2 = 650, 330                  # evidence
X3, W3 = 1070, 300                 # sources / current values
TOTAL_W = X3 + W3 + 20


def _wrap(text, width, max_lines):
    n = max(12, int((width - 2 * PAD) / CHAR_PX))
    lines = textwrap.wrap(str(text), n, break_long_words=True) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:n - 1] + "…"
    return lines


class Box:
    def __init__(self, x, w, cls, label, text, max_lines=5):
        self.x, self.w, self.cls, self.label = x, w, cls, label
        self.lines = _wrap(text, w, max_lines)
        self.h = HEAD + len(self.lines) * LH + PAD
        self.y = 0.0

    @property
    def cy(self):
        return self.y + self.h / 2

    def svg(self):
        t = "".join(f'<text x="{self.x + PAD}" y="{self.y + HEAD + 12 + i * LH:.1f}" class="b">'
                    f'{html.escape(s)}</text>' for i, s in enumerate(self.lines))
        return (f'<g><rect x="{self.x}" y="{self.y:.1f}" width="{self.w}" height="{self.h}" rx="9" '
                f'class="n {self.cls}"/><text x="{self.x + PAD}" y="{self.y + 14:.1f}" class="l">'
                f'{html.escape(self.label)}</text>{t}</g>')


def _edge(x1, y1, x2, y2, label, cls=""):
    c = (x1 + x2) / 2
    marker = "arb" if cls == "bad" else "ar"
    return (f'<path d="M{x1},{y1:.1f} C{c},{y1:.1f} {c},{y2:.1f} {x2},{y2:.1f}" class="e {cls}" '
            f'marker-end="url(#{marker})"/>'
            f'<text x="{c:.1f}" y="{(y1 + y2) / 2 - 5:.1f}" class="el" text-anchor="middle">'
            f'{html.escape(label)}</text>')


def _stack(boxes, desired):
    """Place boxes top-to-bottom as close to their desired centre as possible, no overlaps."""
    order = sorted(range(len(boxes)), key=lambda i: desired[i])
    bottom = 20.0
    for i in order:
        b = boxes[i]
        b.y = max(bottom, desired[i] - b.h / 2)
        bottom = b.y + b.h + GAP


def layout(w: dict):
    """Returns (boxes, edges, height). Separated from rendering so it can be tested."""
    evid = [Box(X2, W2, "stale" if e["stale"] else "ev",
                e["kind"].upper() + (" · STALE (since replaced)" if e["stale"] else ""),
                e["text"], 5) for e in w["evidence"]]
    ev_desired, cur = [], 20.0
    for b in evid:                       # evidence column: simple stack
        b.y = cur
        cur += b.h + GAP
        ev_desired.append(b.cy)

    # column 3: unique source episodes, current-value boxes, older versions
    side, side_want, edges_side = [], [], []
    episodes = {}
    for ev, eb in zip(w["evidence"], evid):
        for d in ev.get("derived_from", []):
            if d["id"] not in episodes:
                episodes[d["id"]] = Box(X3, W3, "ep", "SOURCE EPISODE", d["text"], 8)
                episodes[d["id"]].links = []
                side.append(episodes[d["id"]]); side_want.append(0)
            episodes[d["id"]].links.append(eb)
        if ev["stale"] and ev.get("current_text"):
            cb = Box(X3, W3, "cur", "CURRENT VALUE (replaces the stale fact)", ev["current_text"], 6)
            cb.links, cb.kind = [eb], "supersedes"
            side.append(cb); side_want.append(0)
        for h in ev.get("history", []):
            hb = Box(X3, W3, "old", f"OLDER VERSION v{h['version']}", h["text"], 5)
            hb.links, hb.kind = [eb], "older"
            side.append(hb); side_want.append(0)
    for i, b in enumerate(side):
        side_want[i] = sum(l.cy for l in b.links) / len(b.links)
    _stack(side, side_want)

    mid = sum(ev_desired) / len(ev_desired) if ev_desired else 60.0
    status = w.get("status", "proposed")
    dec = Box(X1, W1, "dc", f"DECISION · {status}", w["summary"], 6)
    dec.y = mid - dec.h / 2
    agent = Box(X0, W0, "ag", "AGENT", w["agent"], 3)
    reviews = w.get("reviews", [])
    if reviews:
        r = reviews[-1]
        ver = Box(X0, W0, "ok" if r["verdict"] == "approved" else "bad",
                  f"VERIFIER · {r['verdict']}", f"{r['agent']}: {r['reason']}", 9)
    else:
        ver = Box(X0, W0, "old", "VERIFIER", "not reviewed yet", 2)
    total = agent.h + 28 + ver.h
    agent.y = mid - total / 2
    ver.y = agent.y + agent.h + 28
    top = min(agent.y, dec.y)
    if top < 20:                         # keep everything on screen
        shift = 20 - top
        for b in (agent, ver, dec):
            b.y += shift

    boxes = evid + side + [dec, agent, ver]
    edges = [_edge(agent.x + agent.w, agent.cy, dec.x, dec.cy - 12, "MADE"),
             _edge(ver.x + ver.w, ver.cy, dec.x, dec.cy + 12, "REVIEWED",
                   "bad" if ver.cls == "bad" else "")]
    for eb, ev in zip(evid, w["evidence"]):
        edges.append(_edge(dec.x + dec.w, dec.cy, eb.x, eb.cy, "BASED_ON",
                           "bad" if ev["stale"] else ""))
    for b in side:
        for eb in b.links:
            kind = getattr(b, "kind", "derived")
            if kind == "supersedes":      # new value -> old value
                edges.append(_edge(b.x, b.cy, eb.x + eb.w, eb.cy, "SUPERSEDES", "bad"))
            elif kind == "older":         # this node is replaced by the evidence
                edges.append(_edge(eb.x + eb.w, eb.cy, b.x, b.cy, "SUPERSEDES"))
            else:
                edges.append(_edge(eb.x + eb.w, eb.cy, b.x, b.cy, "DERIVED_FROM"))
    height = int(max(b.y + b.h for b in boxes) + 30)
    return boxes, edges, height


def trace_html(w: dict) -> str:
    boxes, edges, height = layout(w)
    sub = f"Task: {html.escape(w['task'])} · " if w.get("task") else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Decision trace</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--ag:#e8f0fe;--dc:#fff4d6;--ev:#e6f6ec;--ep:#f3eefc;--old:#eee;--st:#fde8e8;--cur:#d9f2e0;--ln:#8a8a8a;--bad:#d33}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181d;--fg:#eee;--mut:#9aa;--ag:#1d2a44;--dc:#4a3c14;--ev:#1b3a28;--ep:#2e2542;--old:#2a2d33;--st:#4a1f1f;--cur:#1f4a2e;--ln:#888;--bad:#ff6b6b}}}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif}}
h1{{font-size:18px;margin:0 0 4px}}p{{color:var(--mut);margin:0 0 4px;font-size:13px}}
.leg{{margin:0 0 14px}}.wrap{{overflow-x:auto}}
.n{{stroke:var(--ln);stroke-width:1}}.ag{{fill:var(--ag)}}.dc{{fill:var(--dc)}}.ev{{fill:var(--ev)}}
.ep{{fill:var(--ep)}}.old{{fill:var(--old)}}.cur{{fill:var(--cur)}}.ok{{fill:var(--ev)}}
.bad{{fill:var(--st);stroke:var(--bad);stroke-width:2}}.stale{{fill:var(--st);stroke:var(--bad);stroke-width:2}}
.l{{font-size:10px;fill:var(--mut);font-weight:600}}.b{{font-size:12px;fill:var(--fg)}}
.e{{fill:none;stroke:var(--ln);stroke-width:1.4}}.e.bad{{stroke:var(--bad);stroke-width:2;fill:none}}
.el{{font-size:9px;fill:var(--mut);stroke:var(--bg);stroke-width:4px;paint-order:stroke}}
</style></head><body>
<h1>Why did the agent decide this?</h1>
<p>{sub}every box is a node in FalkorDB, every line an edge walked to build this page</p>
<p class="leg">Red = evidence that has changed since the decision was made.</p>
<div class="wrap"><svg width="{TOTAL_W}" height="{height}" viewBox="0 0 {TOTAL_W} {height}">
<defs>
<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#8a8a8a"/></marker>
<marker id="arb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#d33"/></marker>
</defs>
{''.join(edges)}{''.join(b.svg() for b in boxes)}</svg></div></body></html>"""


def write_trace(why: dict, path: str = "trace.html") -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(trace_html(why))
    return path

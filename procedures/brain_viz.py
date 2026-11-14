#!/usr/bin/env python3
"""brain_viz.py -- render YOUR agent's memory graph as one self-contained HTML file.

    python3 procedures/brain_viz.py                 # -> kb/memory_neuron/brain.html
    python3 procedures/brain_viz.py --redact        # word bars instead of text (safe to screenshot/share)
    python3 procedures/brain_viz.py --max-nodes 4000 --out /tmp/brain.html

WHAT IS REAL: nodes + edges come from kb/memory_neuron/l2_graph/memory_graph.json
(built nightly by memory_graph.py). Node size = real degree, colour = real Louvain
community, node kind = the memory filename prefix (decision/insight/pin/...).
Only the "signal" animation timing is random; signals only travel real edges.

WHY A STATIC FILE: no server, no node/npm, no port to expose. Open it in any
browser on any box. Layout is computed HERE (zero LLM, seconds) so the browser
only draws, which keeps a 2-3k node brain smooth on a phone.

PRIVACY: default shows full node text because the file is built on, and for, the
owner's own box. --redact replaces every word with a bar (width 1|2|3 by word
length) BEFORE writing, so the text never enters the file at all -- not hidden
by CSS, absent.

DEPENDENCY: networkx (optional neuron extra). Missing -> this command exits with
a fix hint; nothing else in neuron uses it.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import re
import sys
from pathlib import Path

_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # installer sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WS = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
SRC = WS / "kb/memory_neuron/l2_graph/memory_graph.json"
OUT = WS / "kb/memory_neuron/brain.html"

KINDS = ["memory", "decision", "insight", "relation", "entity", "source",
         "note", "operational", "preference", "pin", "correction", "user"]
KIND_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}(?:-\d+)?_)?_?([a-z]+)")
TOK = re.compile(r"\S+")


def kind_of(fname: str) -> int:
    m = KIND_RE.match(fname or "")
    k = m.group(1) if m else ""
    if k in ("telegram", "discord", "whatsapp", "peer", "peers"):
        k = "user"
    return KINDS.index(k) if k in KINDS else 0


def clean_text(nd: dict) -> str:
    """Readable sentence: snippet minus frontmatter keys, markdown, [valid]/L0 prefixes."""
    t = nd.get("snippet") or nd.get("title") or ""
    t = re.sub(r"<[^>]+>|[*`_>#]{2,}|^-+\s*", " ", t)
    t = re.sub(r"\b(?:type|date|expires|confidence|topics|status|tags)\s*:\s*", " ", t.replace("---", " "), flags=re.I)
    t = re.sub(r"\[(?:valid|uncertain|x| )\]|\bL\d:\s*", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:400]


def bars(text: str, max_words: int = 14) -> str:
    """'1 2 3 ...' -- one bar per word, width bucket by length. No letters survive."""
    out = ["1" if len(w) <= 4 else "2" if len(w) <= 8 else "3" for w in TOK.findall(text)[:max_words]]
    return " ".join(out) or "2"


def pick_subgraph(nx, G, max_nodes: int) -> list:
    """Best-first growth from the biggest hub. A plain top-N-by-degree cut
    shatters the graph into islands; growing along real edges keeps one brain."""
    comp = max(nx.connected_components(G), key=len)
    start = max(comp, key=G.degree)
    seen, heap, keep = {start}, [(-G.degree(start), start)], []
    while heap and len(keep) < max_nodes:
        _, n = heapq.heappop(heap)
        keep.append(n)
        for m in G.neighbors(n):
            if m not in seen:
                seen.add(m)
                heapq.heappush(heap, (-G.degree(m), m))
    return keep


def layout(nx, np, H) -> tuple:
    """Two-level layout: communities on a filled sunflower disk, then each
    community laid out locally in its slot. A flat spring layout on ~2k nodes
    gives a hollow ring; this gives distinct lobes joined by real bridges."""
    comms = sorted(nx.community.louvain_communities(H, seed=7, resolution=1.8), key=len, reverse=True)
    cid = {n: i for i, c in enumerate(comms) for n in c}
    Q = nx.Graph()
    Q.add_nodes_from(range(len(comms)))
    for a, b in H.edges():
        i, j = cid[a], cid[b]
        if i != j:
            Q.add_edge(i, j, w=Q[i][j]["w"] + 1 if Q.has_edge(i, j) else 1)
    for i, j in Q.edges():
        Q[i][j]["w"] = math.log1p(Q[i][j]["w"])  # one fat bridge must not collapse two lobes
    qp0 = nx.spring_layout(Q, weight="w", k=3.0 / math.sqrt(max(1, len(comms))), iterations=300, seed=5)
    A = np.array([qp0[i] for i in range(len(comms))], dtype=float)
    A -= A.mean(0)
    if len(A) > 2:  # whiten: removes the diagonal-band skew of raw spring output
        _, S, Vt = np.linalg.svd(A, full_matrices=False)
        A = A @ Vt.T / np.maximum(S / math.sqrt(len(A)), 1e-9)
    A /= max(np.hypot(A[:, 0], A[:, 1]).max(), 1e-9)
    M, ga = len(A), math.pi * (3 - math.sqrt(5))
    slots = np.array([[math.sqrt((k + .5) / M) * math.cos(k * ga),
                       math.sqrt((k + .5) / M) * math.sin(k * ga)] for k in range(M)])
    free, qp = list(range(M)), {}
    for i in np.argsort(np.hypot(A[:, 0], A[:, 1])):  # innermost first, nearest free slot
        j = int(np.argmin(((slots[free] - A[i]) ** 2).sum(1)))
        qp[int(i)] = slots[free.pop(j)]
    pos = {}
    for i, c in enumerate(comms):
        if len(c) == 1:
            pos[next(iter(c))] = tuple(qp[i])
            continue
        lp = nx.spring_layout(H.subgraph(c), seed=i, iterations=60, k=1.5 / math.sqrt(len(c)))
        r = 0.02 * math.sqrt(len(c))
        for n, p in lp.items():
            pos[n] = (qp[i][0] + p[0] * r, qp[i][1] + p[1] * r)
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half = max(max(xs) - min(xs), max(ys) - min(ys)) / 2 or 1.0
    return {n: ((p[0] - cx) / half, (p[1] - cy) / half) for n, p in pos.items()}, cid, len(comms)


def build(data: dict, max_nodes: int, max_edges: int, redact: bool) -> dict:
    import networkx as nx
    import numpy as np
    nodes, edges = data["nodes"], data["edges"]
    G = nx.Graph()
    for e in edges:
        a, b = e["a"], e["b"]
        if a == b:
            continue
        w = float(e.get("neural_score") or e.get("sim") or 1.0)
        if not G.has_edge(a, b) or G[a][b]["w"] < w:
            G.add_edge(a, b, w=w)
    if G.number_of_nodes() == 0:
        raise ValueError("memory graph has no edges yet -- let memory_graph.py run first")
    H = G.subgraph(pick_subgraph(nx, G, max_nodes)).copy()
    xy, cid, ncomm = layout(nx, np, H)
    order = list(H.nodes)
    ix = {n: i for i, n in enumerate(order)}
    byidx = {nd.get("idx", i): nd for i, nd in enumerate(nodes)}
    out_nodes = []
    for n in order:
        nd = byidx.get(n) or {}
        text = clean_text(nd)
        out_nodes.append([round(xy[n][0], 4), round(xy[n][1], 4), cid[n], H.degree(n),
                          kind_of(nd.get("file", "")), bars(text) if redact else text])
    E = sorted(H.edges(data="w"), key=lambda t: -t[2])[:max_edges]
    return {
        "redact": redact,
        "kinds": KINDS,
        "totals": {"nodes": data.get("node_count", len(nodes)), "edges": data.get("edge_count", len(edges)),
                   "relations": data.get("relation_edge_count", 0), "entities": data.get("entity_node_count", 0),
                   "clusters": ncomm, "shown": len(order), "generated": data.get("generated", "")},
        "n": out_nodes,
        "e": [[ix[a], ix[b]] for a, b, _ in E],
    }


def to_html(payload: dict) -> str:
    # "</" must not appear inside <script>: escape so node text can never close the tag.
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return HTML.replace("__DATA__", blob)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--redact", action="store_true", help="replace every word with a bar (shareable)")
    ap.add_argument("--max-nodes", type=int, default=2400)
    ap.add_argument("--max-edges", type=int, default=7000)
    a = ap.parse_args()
    try:
        import networkx  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        print(f"brain_viz: {e.name} not installed. Fix: {sys.executable} -m pip install networkx numpy "
              "(add --break-system-packages if PEP-668)", file=sys.stderr)
        return 2
    if not Path(a.src).is_file():
        print(f"brain_viz: {a.src} missing -- run: python3 {WS}/procedures/memory_graph.py", file=sys.stderr)
        return 1
    try:
        payload = build(json.load(open(a.src, encoding="utf-8")), a.max_nodes, a.max_edges, a.redact)
    except ValueError as e:
        print(f"brain_viz: {e}", file=sys.stderr)
        return 1
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(to_html(payload), encoding="utf-8")
    os.replace(tmp, out)
    t = payload["totals"]
    print(f"brain_viz: {t['shown']} of {t['nodes']} nodes, {len(payload['e'])} edges, {t['clusters']} clusters"
          f"{' (redacted)' if a.redact else ''} -> {out} ({out.stat().st_size // 1024} KB)")
    return 0


HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dinomem brain</title>
<style>
html,body{margin:0;height:100%;background:#07080d;color:#cfd3e0;font:13px/1.4 ui-sans-serif,system-ui,sans-serif;overflow:hidden}
canvas{display:block;width:100vw;height:100vh;touch-action:none;cursor:grab}
#hud{position:fixed;left:12px;top:10px;pointer-events:none}
#hud b{color:#fff;font-size:15px}#hud span{opacity:.65}
#ctl{position:fixed;right:12px;top:10px;display:flex;gap:6px}
#ctl button{background:#151826;color:#cfd3e0;border:1px solid #2a2f45;border-radius:6px;width:30px;height:30px;font-size:16px;cursor:pointer}
#card{position:fixed;left:12px;bottom:12px;max-width:min(460px,calc(100vw - 24px));background:#11131ecc;border:1px solid #2a2f45;border-radius:10px;padding:10px 12px;display:none;backdrop-filter:blur(6px)}
#card .k{font-size:11px;text-transform:uppercase;letter-spacing:.08em;opacity:.7}
.bar{display:inline-block;height:9px;border-radius:3px;background:#3a4060;margin:0 4px 2px 0;vertical-align:middle}
</style></head><body>
<canvas id="c"></canvas>
<div id="hud"></div>
<div id="ctl"><button id="zi">+</button><button id="zo">&minus;</button><button id="zr">&#10227;</button></div>
<div id="card"></div>
<script>
const D=__DATA__;
const PAL=["#7aa2ff","#ff7ab6","#7affc4","#ffd36e","#b38cff","#6ee7ff","#ff9f6e","#a8ff6e","#ff6e6e","#6effa0","#e0a8ff","#6eb4ff"];
const N=D.n,E=D.e,T=D.totals,c=document.getElementById("c"),g=c.getContext("2d");
const adj=N.map(()=>[]);E.forEach(([a,b],i)=>{adj[a].push(b);adj[b].push(a)});
const fmt=x=>Number(x).toLocaleString();
document.getElementById("hud").innerHTML=`<b>dinomem brain</b><br><span>${fmt(T.nodes)} neurons &middot; ${fmt(T.edges)} synapses &middot; ${fmt(T.relations)} relations &middot; ${T.clusters} clusters<br>showing ${fmt(T.shown)} &middot; ${T.generated}${D.redact?" &middot; redacted":""}</span>`;
let W,H,dpr,s=1,ox=0,oy=0;
function size(){dpr=Math.min(2,devicePixelRatio||1);W=innerWidth;H=innerHeight;c.width=W*dpr;c.height=H*dpr}
size();addEventListener("resize",size);
const R=()=>Math.min(W,H)*0.46*s;
const sx=x=>W/2+ox+x*R(),sy=y=>H/2+oy+y*R();
const rad=d=>(1.2+Math.sqrt(d)*0.55)*Math.sqrt(s);
let pulses=[],sel=-1,run=true;
function spawn(){if(pulses.length>90)return;const i=(Math.random()*E.length)|0;const[a,b]=E[i];pulses.push({a,b,t:0,v:.012+Math.random()*.02,hop:2+(Math.random()*3|0)})}
function draw(){
 g.setTransform(dpr,0,0,dpr,0,0);g.fillStyle="#07080d";g.fillRect(0,0,W,H);
 g.lineWidth=.5;g.globalAlpha=.10;g.strokeStyle="#8a93b8";g.beginPath();
 for(const[a,b]of E){g.moveTo(sx(N[a][0]),sy(N[a][1]));g.lineTo(sx(N[b][0]),sy(N[b][1]))}
 g.stroke();g.globalAlpha=1;
 g.globalCompositeOperation="lighter";
 for(let i=0;i<N.length;i++){const n=N[i];g.fillStyle=PAL[n[2]%12];g.globalAlpha=.55;g.beginPath();g.arc(sx(n[0]),sy(n[1]),rad(n[3]),0,7);g.fill()}
 g.globalAlpha=1;
 for(const p of pulses){const A=N[p.a],B=N[p.b],x=sx(A[0]+(B[0]-A[0])*p.t),y=sy(A[1]+(B[1]-A[1])*p.t);
  g.fillStyle="#fff";g.beginPath();g.arc(x,y,1.8*Math.sqrt(s),0,7);g.fill();
  g.fillStyle=PAL[A[2]%12];g.globalAlpha=.35;g.beginPath();g.arc(x,y,5*Math.sqrt(s),0,7);g.fill();g.globalAlpha=1}
 g.globalCompositeOperation="source-over";
 if(sel>=0){const n=N[sel];g.strokeStyle="#fff";g.lineWidth=1.5;g.beginPath();g.arc(sx(n[0]),sy(n[1]),rad(n[3])+4,0,7);g.stroke();
  g.globalAlpha=.6;g.lineWidth=1;g.beginPath();for(const m of adj[sel]){g.moveTo(sx(n[0]),sy(n[1]));g.lineTo(sx(N[m][0]),sy(N[m][1]))}g.stroke();g.globalAlpha=1}
}
function step(){
 if(run){if(Math.random()<.5)spawn();
  for(const p of pulses){p.t+=p.v;if(p.t>=1&&p.hop>0&&adj[p.b].length){const nx=adj[p.b][(Math.random()*adj[p.b].length)|0];p.a=p.b;p.b=nx;p.t=0;p.hop--}}
  pulses=pulses.filter(p=>p.t<1);draw()}
 requestAnimationFrame(step)}
document.addEventListener("visibilitychange",()=>{run=!document.hidden});
function zoom(f,cx=W/2,cy=H/2){const ns=Math.max(.3,Math.min(40,s*f));f=ns/s;ox=(ox+W/2-cx)*f-W/2+cx;oy=(oy+H/2-cy)*f-H/2+cy;s=ns;draw()}
document.getElementById("zi").onclick=()=>zoom(1.4);
document.getElementById("zo").onclick=()=>zoom(1/1.4);
document.getElementById("zr").onclick=()=>{s=1;ox=oy=0;draw()};
c.addEventListener("wheel",e=>{e.preventDefault();zoom(Math.exp(-e.deltaY*.0015),e.clientX,e.clientY)},{passive:false});
const pts=new Map();let moved=0,pinch=0;
c.addEventListener("pointerdown",e=>{c.setPointerCapture(e.pointerId);pts.set(e.pointerId,[e.clientX,e.clientY]);moved=0});
c.addEventListener("pointermove",e=>{if(!pts.has(e.pointerId))return;const[px,py]=pts.get(e.pointerId);
 if(pts.size===2){const o=[...pts.values()];const d0=Math.hypot(o[0][0]-o[1][0],o[0][1]-o[1][1]);pts.set(e.pointerId,[e.clientX,e.clientY]);
  const q=[...pts.values()];const d1=Math.hypot(q[0][0]-q[1][0],q[0][1]-q[1][1]);if(d0>0)zoom(d1/d0,(q[0][0]+q[1][0])/2,(q[0][1]+q[1][1])/2);moved=9;return}
 ox+=e.clientX-px;oy+=e.clientY-py;moved+=Math.abs(e.clientX-px)+Math.abs(e.clientY-py);pts.set(e.pointerId,[e.clientX,e.clientY]);draw()});
const up=e=>{pts.delete(e.pointerId);if(moved<5&&pts.size===0)pick(e.clientX,e.clientY)};
c.addEventListener("pointerup",up);c.addEventListener("pointercancel",e=>pts.delete(e.pointerId));
const esc=t=>t.replace(/[&<>"]/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[ch]));
function body(t){return D.redact?t.split(" ").map(w=>`<span class="bar" style="width:${w*14}px"></span>`).join(""):esc(t)}
function pick(x,y){let best=-1,bd=1e9;for(let i=0;i<N.length;i++){const d=Math.hypot(sx(N[i][0])-x,sy(N[i][1])-y);if(d<bd){bd=d;best=i}}
 const card=document.getElementById("card");
 if(bd>Math.max(12,rad(N[best][3])+6)){sel=-1;card.style.display="none";draw();return}
 sel=best;const n=N[best];
 card.innerHTML=`<div class="k" style="color:${PAL[n[2]%12]}">${D.kinds[n[4]]} &middot; ${n[3]} synapses &middot; cluster ${n[2]+1}</div><div style="margin-top:6px">${body(n[5])}</div>`;
 card.style.display="block";draw()}
draw();requestAnimationFrame(step);
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())

/**
 * components/GraphCanvas.tsx — the force layout, drawn on a canvas.
 *
 * Canvas rather than SVG, and physics outside React. Both are performance
 * decisions with a visible consequence:
 *
 *   - A graph of 400 nodes and 1,500 edges is 1,900 SVG elements. Re-rendering
 *     that tree sixty times a second to move some dots is how a graph tab ends
 *     up at four frames per second. On a canvas the same frame is two `stroke()`
 *     calls and one `fill()` per colour — under a millisecond.
 *   - The simulation lives in a ref and is stepped inside `requestAnimationFrame`.
 *     React never sees a tick. It re-renders when the *data* changes, which is
 *     the only time the DOM has anything new to say.
 *
 * The layout is deterministic. Seed positions come from a golden-angle spiral
 * rather than `Math.random()`, so opening the same graph twice produces the same
 * picture — a graph that reshuffles every visit teaches you nothing about shape,
 * and "wait, where did that cluster go" is a real cost.
 *
 * Repulsion is bucketed into a uniform grid: every body pushes only against
 * bodies within one cell of it, and a global pull toward the centre stands in
 * for the far field. That turns an O(n²) tick into something closer to O(n),
 * which is the difference between 400 nodes being smooth and being a slideshow.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Crosshair, Maximize2, Minus, Pause, Play, Plus } from 'lucide-react';
import type { GraphEdge, GraphNode } from '../types';
import { color, nodeProp } from '../lib/kinds';
import { useSize } from '../lib/useFetch';

export interface GraphCanvasProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  onExpand: (id: string) => void;
  /** Ids the caller wants pulled out of the crowd — search matches, usually. */
  marked?: Set<string>;
}

interface Body {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
  m: number;
  color: string;
  label: string;
  weight: number;
  pinned: boolean;
}

const DAMP = 0.84;
const ALPHA_MIN = 0.02;
const ALPHA_DECAY = 0.985;
const CELL = 90;
const REPEL = 900;
const GRAVITY = 0.0011;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const MAX_LABELS = 90;

export default function GraphCanvas({
  nodes,
  edges,
  selected,
  onSelect,
  onExpand,
  marked,
}: GraphCanvasProps) {
  const wrap = useRef<HTMLDivElement | null>(null);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const size = useSize(wrap);

  const bodies = useRef<Map<string, Body>>(new Map());
  const list = useRef<Body[]>([]);
  const links = useRef<Array<{ a: Body; b: Body; w: number }>>([]);
  const view = useRef({ x: 0, y: 0, k: 1 });
  const alpha = useRef(1);
  const drag = useRef<{ body: Body | null; dx: number; dy: number; panning: boolean } | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const hoverRef = useRef<string | null>(null);
  const [running, setRunning] = useState(true);
  const runRef = useRef(true);
  const dirty = useRef(true);
  const fitWanted = useRef(true);

  useEffect(() => {
    runRef.current = running;
    if (running) alpha.current = Math.max(alpha.current, 0.35);
    dirty.current = true;
  }, [running]);

  // Neighbours of the selection, so everything else can recede.
  const near = useMemo(() => {
    if (!selected) return null;
    const s = new Set<string>([selected]);
    for (const e of edges) {
      if (e.src === selected) s.add(e.dst);
      else if (e.dst === selected) s.add(e.src);
    }
    return s;
  }, [selected, edges]);
  const nearRef = useRef<Set<string> | null>(null);
  useEffect(() => {
    nearRef.current = near;
    dirty.current = true;
  }, [near]);

  const markedRef = useRef<Set<string> | null>(null);
  useEffect(() => {
    markedRef.current = marked || null;
    dirty.current = true;
  }, [marked]);

  const selRef = useRef<string | null>(null);
  useEffect(() => {
    selRef.current = selected;
    dirty.current = true;
  }, [selected]);

  // ── build the simulation when the data changes ──────────────────────────
  // Positions of nodes that were already on screen are kept, so expanding a
  // node adds to the picture instead of replacing it. That is the difference
  // between exploring and starting over.
  useEffect(() => {
    const prev = bodies.current;
    const next = new Map<string, Body>();
    const cx = 0;
    const cy = 0;

    nodes.forEach((n, i) => {
      const had = prev.get(n.id);
      const r = Math.min(26, Math.max(3.5, 4 + Math.sqrt(Math.max(0, n.weight)) * 1.3));
      const paint = color(nodeProp(n)) || '#8b95a5';
      if (had) {
        had.r = r;
        had.color = paint;
        had.label = n.label;
        had.weight = n.weight;
        next.set(n.id, had);
        return;
      }
      const d = Math.sqrt(i + 1) * 20;
      next.set(n.id, {
        id: n.id,
        x: cx + d * Math.cos(i * GOLDEN),
        y: cy + d * Math.sin(i * GOLDEN),
        vx: 0,
        vy: 0,
        r,
        m: r,
        color: paint,
        label: n.label,
        weight: n.weight,
        pinned: false,
      });
    });

    bodies.current = next;
    list.current = [...next.values()];
    links.current = [];
    for (const e of edges) {
      const a = next.get(e.src);
      const b = next.get(e.dst);
      if (a && b) links.current.push({ a, b, w: e.weight || 1 });
    }
    alpha.current = 1;
    fitWanted.current = true;
    dirty.current = true;
  }, [nodes, edges]);

  // ── one physics tick ────────────────────────────────────────────────────
  const step = useCallback(() => {
    const bods = list.current;
    const n = bods.length;
    if (!n) return;
    const a = alpha.current;

    // Bucket for repulsion. Rebuilt each tick — allocating one Map of small
    // arrays is far cheaper than the n² it replaces.
    const grid = new Map<number, Body[]>();
    const key = (gx: number, gy: number) => gx * 73856093 + gy * 19349663;
    for (const b of bods) {
      const gx = Math.floor(b.x / CELL);
      const gy = Math.floor(b.y / CELL);
      const k = key(gx, gy);
      const cell = grid.get(k);
      if (cell) cell.push(b);
      else grid.set(k, [b]);
    }

    for (const b of bods) {
      let fx = 0;
      let fy = 0;
      const gx = Math.floor(b.x / CELL);
      const gy = Math.floor(b.y / CELL);
      for (let ox = -1; ox <= 1; ox += 1) {
        for (let oy = -1; oy <= 1; oy += 1) {
          const cell = grid.get(key(gx + ox, gy + oy));
          if (!cell) continue;
          for (const o of cell) {
            if (o === b) continue;
            let dx = b.x - o.x;
            let dy = b.y - o.y;
            let d2 = dx * dx + dy * dy;
            if (d2 > CELL * CELL * 4) continue;
            if (d2 < 0.01) {
              // Two bodies exactly on top of each other have no direction to
              // separate along; nudge deterministically by id order.
              dx = b.id < o.id ? 0.5 : -0.5;
              dy = 0.31;
              d2 = 0.35;
            }
            const d = Math.sqrt(d2);
            const f = (REPEL * (b.m + o.m)) / (d2 * 20);
            fx += (dx / d) * f;
            fy += (dy / d) * f;
          }
        }
      }
      // The centre pull replaces the far field the grid throws away.
      fx -= b.x * GRAVITY * 60;
      fy -= b.y * GRAVITY * 60;
      b.vx = (b.vx + fx) * DAMP;
      b.vy = (b.vy + fy) * DAMP;
    }

    for (const l of links.current) {
      const dx = l.b.x - l.a.x;
      const dy = l.b.y - l.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      // Heavier links sit closer: weight is how many rows assert the link, so
      // "asserted eleven times" should read as a tighter bond than "once".
      const rest = 74 + 40 / (1 + Math.log2(1 + l.w)) + l.a.r + l.b.r;
      const f = (d - rest) * 0.045;
      const ux = (dx / d) * f;
      const uy = (dy / d) * f;
      l.a.vx += ux;
      l.a.vy += uy;
      l.b.vx -= ux;
      l.b.vy -= uy;
    }

    for (const b of bods) {
      if (b.pinned) {
        b.vx = 0;
        b.vy = 0;
        continue;
      }
      b.x += b.vx * a;
      b.y += b.vy * a;
    }

    alpha.current = Math.max(0, a * ALPHA_DECAY);
    dirty.current = true;
  }, []);

  const fit = useCallback(() => {
    const bods = list.current;
    const { w, h } = size;
    if (!bods.length || !w || !h) return;
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    for (const b of bods) {
      x0 = Math.min(x0, b.x - b.r);
      y0 = Math.min(y0, b.y - b.r);
      x1 = Math.max(x1, b.x + b.r);
      y1 = Math.max(y1, b.y + b.r);
    }
    const pad = 48;
    const k = Math.min(4, Math.max(0.12, Math.min((w - pad * 2) / (x1 - x0 || 1), (h - pad * 2) / (y1 - y0 || 1))));
    view.current = {
      k,
      x: w / 2 - ((x0 + x1) / 2) * k,
      y: h / 2 - ((y0 + y1) / 2) * k,
    };
    dirty.current = true;
  }, [size]);

  // ── draw ────────────────────────────────────────────────────────────────
  const draw = useCallback(() => {
    const el = canvas.current;
    const { w, h } = size;
    if (!el || !w || !h) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (el.width !== Math.round(w * dpr) || el.height !== Math.round(h * dpr)) {
      el.width = Math.round(w * dpr);
      el.height = Math.round(h * dpr);
    }
    const ctx = el.getContext('2d');
    if (!ctx) return;
    const v = view.current;
    const sel = selRef.current;
    const ring = nearRef.current;
    const hov = hoverRef.current;
    const mark = markedRef.current;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    // Every custom property read on this canvas — here and in the four blocks
    // below — has to be a name `tokens.css` actually defines. `resolveColor`
    // returns "" for an unknown property, so a typo or an invented name does not
    // fail: it quietly takes the `||` fallback, and a fallback is a second
    // palette that nothing keeps in step. Six of these used to read `--line`,
    // `--ink`, `--bg` and `--accent`, none of which exist in this design system,
    // so the search ring was drawing gold on an indigo app and every label wore
    // a #0e1116 halo over a #060608 background.
    const ink = color('--g-line') || 'rgba(140,150,165,0.35)';
    const inkSoft = color('--g-line-soft') || 'rgba(140,150,165,0.14)';

    // Edges: one path for the background, one for the selection's own links.
    ctx.lineWidth = 1;
    ctx.strokeStyle = sel ? inkSoft : ink;
    ctx.beginPath();
    for (const l of links.current) {
      if (sel && (l.a.id === sel || l.b.id === sel)) continue;
      ctx.moveTo(l.a.x * v.k + v.x, l.a.y * v.k + v.y);
      ctx.lineTo(l.b.x * v.k + v.x, l.b.y * v.k + v.y);
    }
    ctx.stroke();

    if (sel) {
      const me = bodies.current.get(sel);
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = me?.color || ink;
      ctx.globalAlpha = 0.9;
      ctx.beginPath();
      for (const l of links.current) {
        if (l.a.id !== sel && l.b.id !== sel) continue;
        ctx.moveTo(l.a.x * v.k + v.x, l.a.y * v.k + v.y);
        ctx.lineTo(l.b.x * v.k + v.x, l.b.y * v.k + v.y);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Nodes, grouped by colour so the whole graph is a handful of fills.
    const byColor = new Map<string, Body[]>();
    for (const b of list.current) {
      const g = byColor.get(b.color);
      if (g) g.push(b);
      else byColor.set(b.color, [b]);
    }
    // `hue`, not `color` — `color()` is the memoised custom-property reader
    // imported above, and shadowing it here would be a trap for the next edit.
    for (const [hue, group] of byColor) {
      ctx.fillStyle = hue;
      // Dimmed pass first, then the full-strength one — two fills per colour
      // instead of one per node.
      if (ring) {
        ctx.globalAlpha = 0.16;
        ctx.beginPath();
        for (const b of group) {
          if (ring.has(b.id)) continue;
          const r = Math.max(1, b.r * v.k);
          ctx.moveTo(b.x * v.k + v.x + r, b.y * v.k + v.y);
          ctx.arc(b.x * v.k + v.x, b.y * v.k + v.y, r, 0, Math.PI * 2);
        }
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      ctx.beginPath();
      for (const b of group) {
        if (ring && !ring.has(b.id)) continue;
        const r = Math.max(1, b.r * v.k);
        ctx.moveTo(b.x * v.k + v.x + r, b.y * v.k + v.y);
        ctx.arc(b.x * v.k + v.x, b.y * v.k + v.y, r, 0, Math.PI * 2);
      }
      ctx.fill();
    }

    // Search matches get a ring rather than a colour — colour is spoken for.
    if (mark && mark.size) {
      ctx.strokeStyle = color('--accent-primary') || '#818cf8';
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (const b of list.current) {
        if (!mark.has(b.id)) continue;
        const r = Math.max(2, b.r * v.k) + 3;
        ctx.moveTo(b.x * v.k + v.x + r, b.y * v.k + v.y);
        ctx.arc(b.x * v.k + v.x, b.y * v.k + v.y, r, 0, Math.PI * 2);
      }
      ctx.stroke();
    }

    for (const id of [hov, sel]) {
      if (!id) continue;
      const b = bodies.current.get(id);
      if (!b) continue;
      ctx.strokeStyle = color('--text-primary') || '#f0f0f6';
      ctx.lineWidth = id === sel ? 2 : 1.2;
      ctx.beginPath();
      ctx.arc(b.x * v.k + v.x, b.y * v.k + v.y, Math.max(2, b.r * v.k) + 3.5, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Labels last, in screen space so they stay legible at any zoom.
    const labelled = list.current
      .filter((b) => b.r * v.k >= 7 || b.id === sel || b.id === hov || (ring && ring.has(b.id)))
      .sort((a, b) => b.weight - a.weight)
      .slice(0, MAX_LABELS);
    ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (const b of labelled) {
      const sx = b.x * v.k + v.x;
      const sy = b.y * v.k + v.y + Math.max(2, b.r * v.k) + 3;
      if (sx < -80 || sx > w + 80 || sy < -20 || sy > h + 20) continue;
      const text = b.label.length > 26 ? `${b.label.slice(0, 25)}…` : b.label;
      ctx.globalAlpha = ring && !ring.has(b.id) ? 0.25 : 1;
      ctx.lineWidth = 3;
      ctx.strokeStyle = color('--bg-deep') || '#060608';
      ctx.strokeText(text, sx, sy);
      ctx.fillStyle = color('--text-secondary') || '#a3a3b5';
      ctx.fillText(text, sx, sy);
    }
    ctx.globalAlpha = 1;
    dirty.current = false;
  }, [size]);

  // ── the loop ────────────────────────────────────────────────────────────
  useEffect(() => {
    let raf = 0;
    const frame = () => {
      raf = requestAnimationFrame(frame);
      if (runRef.current && alpha.current > ALPHA_MIN) {
        step();
        if (fitWanted.current && alpha.current < 0.55) {
          fitWanted.current = false;
          fit();
        }
      }
      if (dirty.current) draw();
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [step, draw, fit]);

  useEffect(() => {
    dirty.current = true;
  }, [size]);

  // ── pointer ─────────────────────────────────────────────────────────────
  const at = useCallback((e: React.PointerEvent | React.MouseEvent) => {
    const el = canvas.current;
    if (!el) return { x: 0, y: 0 };
    const box = el.getBoundingClientRect();
    return { x: e.clientX - box.left, y: e.clientY - box.top };
  }, []);

  const pick = useCallback((sx: number, sy: number): Body | null => {
    const v = view.current;
    let best: Body | null = null;
    let bestD = Infinity;
    for (const b of list.current) {
      const dx = b.x * v.k + v.x - sx;
      const dy = b.y * v.k + v.y - sy;
      const d = Math.sqrt(dx * dx + dy * dy);
      const hit = Math.max(6, b.r * v.k + 4);
      if (d <= hit && d < bestD) {
        best = b;
        bestD = d;
      }
    }
    return best;
  }, []);

  const onDown = (e: React.PointerEvent) => {
    const { x, y } = at(e);
    const hitBody = pick(x, y);
    (e.target as Element).setPointerCapture?.(e.pointerId);
    if (hitBody) {
      const v = view.current;
      drag.current = {
        body: hitBody,
        dx: hitBody.x * v.k + v.x - x,
        dy: hitBody.y * v.k + v.y - y,
        panning: false,
      };
      hitBody.pinned = true;
      alpha.current = Math.max(alpha.current, 0.4);
    } else {
      drag.current = { body: null, dx: x - view.current.x, dy: y - view.current.y, panning: true };
    }
  };

  const onMove = (e: React.PointerEvent) => {
    const { x, y } = at(e);
    const d = drag.current;
    if (d?.panning) {
      view.current.x = x - d.dx;
      view.current.y = y - d.dy;
      dirty.current = true;
      return;
    }
    if (d?.body) {
      const v = view.current;
      d.body.x = (x + d.dx - v.x) / v.k;
      d.body.y = (y + d.dy - v.y) / v.k;
      d.body.vx = 0;
      d.body.vy = 0;
      alpha.current = Math.max(alpha.current, 0.3);
      dirty.current = true;
      return;
    }
    const over = pick(x, y);
    const id = over?.id || null;
    if (id !== hoverRef.current) {
      hoverRef.current = id;
      setHover(id);
      dirty.current = true;
    }
  };

  const onUp = (e: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    if (d?.body) {
      // Shift-release leaves it pinned; otherwise it rejoins the simulation.
      d.body.pinned = e.shiftKey;
      alpha.current = Math.max(alpha.current, 0.3);
    }
  };

  const onClick = (e: React.MouseEvent) => {
    const { x, y } = at(e);
    const hitBody = pick(x, y);
    onSelect(hitBody ? hitBody.id : null);
  };

  const onDouble = (e: React.MouseEvent) => {
    const { x, y } = at(e);
    const hitBody = pick(x, y);
    if (hitBody) onExpand(hitBody.id);
  };

  const onWheel = (e: React.WheelEvent) => {
    const { x, y } = at(e);
    const v = view.current;
    const k = Math.min(4, Math.max(0.12, v.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    // Zoom about the pointer, so the thing under the cursor stays under it.
    view.current = { k, x: x - ((x - v.x) / v.k) * k, y: y - ((y - v.y) / v.k) * k };
    dirty.current = true;
  };

  const zoomBy = (f: number) => {
    const v = view.current;
    const k = Math.min(4, Math.max(0.12, v.k * f));
    const cx = size.w / 2;
    const cy = size.h / 2;
    view.current = { k, x: cx - ((cx - v.x) / v.k) * k, y: cy - ((cy - v.y) / v.k) * k };
    dirty.current = true;
  };

  return (
    <div className="gcanvas" ref={wrap}>
      <canvas
        ref={canvas}
        style={{ width: size.w || '100%', height: size.h || '100%' }}
        className={hover ? 'is-over' : undefined}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onPointerLeave={() => {
          hoverRef.current = null;
          setHover(null);
          dirty.current = true;
        }}
        onClick={onClick}
        onDoubleClick={onDouble}
        onWheel={onWheel}
      />
      <div className="gctl">
        <button className="btn-icon" onClick={() => zoomBy(1.25)} title="zoom in">
          <Plus size={13} />
        </button>
        <button className="btn-icon" onClick={() => zoomBy(1 / 1.25)} title="zoom out">
          <Minus size={13} />
        </button>
        <button className="btn-icon" onClick={fit} title="fit everything on screen">
          <Maximize2 size={13} />
        </button>
        <button
          className="btn-icon"
          onClick={() => {
            alpha.current = 1;
            setRunning(true);
          }}
          title="shake the layout loose and let it settle again"
        >
          <Crosshair size={13} />
        </button>
        <button
          className="btn-icon"
          onClick={() => setRunning((r) => !r)}
          title={running ? 'freeze the layout' : 'let it move again'}
        >
          {running ? <Pause size={13} /> : <Play size={13} />}
        </button>
      </div>
      <div className="ghint">
        drag to move · scroll to zoom · click to inspect · double-click to pull in its
        neighbours · shift-release to pin
      </div>
    </div>
  );
}

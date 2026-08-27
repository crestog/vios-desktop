/**
 * views/Graph.tsx — the archive as a shape, and every line traceable to a row.
 *
 * The point of this screen is not that it looks like a graph. It is that a
 * connection between two things is a *claim*, and every claim here can be
 * opened until it bottoms out in the rows that made it true. `graph_edges`
 * stores `ref` as `table|column[|value]`, which is enough to rebuild the query
 * that produced the edge — so clicking a relationship shows the SQL's answer,
 * not a tooltip repeating what the line already showed.
 *
 * Four decisions worth reading before editing:
 *
 *   - **Expanding adds; it does not replace.** Double-clicking a node merges its
 *     neighbourhood into the picture and `GraphCanvas` keeps the positions of
 *     everything already on screen. Exploration is cumulative, which is the
 *     difference between building a mental map and being shown a slideshow of
 *     unrelated stars. "Start over here" exists for when the picture has grown
 *     past usefulness.
 *
 *   - **The address bar carries the exploration.** `?node=` focuses a node,
 *     `?keys=` builds the graph from a set of videos (that is the bridge from a
 *     search result page), `?hide=` and `?min=` carry the filters. Selection is
 *     written with `replace`, so Back leaves the graph rather than walking back
 *     through forty clicks.
 *
 *   - **Search marks; it does not recolour.** A hue in this application means
 *     one thing — which observer produced a claim — so a match gets a ring.
 *     `lib/kinds.ts` owns the rest of that rule.
 *
 *   - **The fan-out is visible and adjustable.** `graph.neighbors()` sorts by
 *     weight and truncates, and it reports how many it dropped. A view that
 *     silently showed 60 of 812 connections would be lying about the shape of
 *     the archive, so the count is printed and the limit is a control.
 */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowRight,
  ChevronDown,
  ChevronRight,
  Focus,
  Link2,
  Play,
  RefreshCw,
  Route as RouteIcon,
  Search as SearchIcon,
  X,
} from 'lucide-react';
import type { GraphEdge, GraphNode, RecordSet } from '../types';
import type { ViewProps } from '../lib/router';
import { go, href, num } from '../lib/router';
import {
  expandNode,
  findNodes,
  findPath,
  getEdge,
  getGraph,
  getNode,
  graphFromVideos,
  posterUrl,
  rebuildGraph,
} from '../lib/api';
import { store } from '../lib/store';
import { useDebounced, useFetch } from '../lib/useFetch';
import { clip, fmtCompact, fmtCount, fmtDur, plural } from '../lib/format';
import { nodeCss, nodeNote, nodeProp, nodeTypeLabel } from '../lib/kinds';
import GraphCanvas from '../components/GraphCanvas';

/** `graph.FANOUT` — the server's own default, repeated so the control shows it. */
const FANOUT = 60;
const HUBS = 22;
const EDGE_ROWS = 40;

interface Picture {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

const EMPTY: Picture = { nodes: [], edges: [] };

/** An edge's identity is its primary key: `(src, dst, rel)`. */
// `\u0000` written as an escape rather than as the byte itself: a literal
// NUL in a source file makes every text tool treat this module as binary,
// and the separator only has to be a character that cannot occur in an id
// or a column name — which is exactly what it is either way.
const ekey = (e: GraphEdge) => `${e.src}\u0000${e.dst}\u0000${e.rel}`;

function merge(base: Picture, add: Partial<Picture> | null | undefined): Picture {
  if (!add || (!add.nodes?.length && !add.edges?.length)) return base;
  const nodes = new Map(base.nodes.map((n) => [n.id, n]));
  let grew = false;
  for (const n of add.nodes || []) {
    if (!nodes.has(n.id)) {
      nodes.set(n.id, n);
      grew = true;
    }
  }
  const seen = new Set(base.edges.map(ekey));
  const edges = base.edges.slice();
  for (const e of add.edges || []) {
    const k = ekey(e);
    if (seen.has(k)) continue;
    seen.add(k);
    edges.push(e);
    grew = true;
  }
  // Same identity when nothing was added, so the canvas does not rebuild its
  // simulation for an expansion that returned only nodes already on screen.
  return grew ? { nodes: [...nodes.values()], edges } : base;
}

export default function GraphView({ route }: ViewProps) {
  const p = route.params;
  const focus = p.get('node') || '';
  const keysParam = p.get('keys') || '';
  const hubs = num(p, 'hubs') ?? HUBS;
  const minW = num(p, 'min') ?? 0;
  const fan = num(p, 'fan') ?? FANOUT;
  const hideParam = p.get('hide') || '';

  const keyList = useMemo(() => keysParam.split(',').filter(Boolean), [keysParam]);
  const hidden = useMemo(() => new Set(hideParam.split(',').filter(Boolean)), [hideParam]);

  const [pic, setPic] = useState<Picture>(EMPTY);
  const [selected, setSelected] = useState<string | null>(focus || null);
  const [pending, setPending] = useState(0);
  const [truncated, setTruncated] = useState<{ id: string; shown: number; total: number } | null>(
    null
  );
  const [find, setFind] = useState('');
  const [pathFrom, setPathFrom] = useState<GraphNode | null>(null);
  const [rebuilding, setRebuilding] = useState<'' | 'ask' | 'busy'>('');
  const [rebuilt, setRebuilt] = useState<string | null>(null);

  // ── the base picture ─────────────────────────────────────────────────────
  const base = useFetch(
    (signal) =>
      keyList.length
        ? graphFromVideos(keyList, Math.max(keyList.length, 24), signal)
        : getGraph(hubs, signal),
    [keysParam, hubs]
  );

  useEffect(() => {
    if (!base.data) return;
    setPic({ nodes: base.data.nodes || [], edges: base.data.edges || [] });
    setTruncated(null);
  }, [base.data]);

  useEffect(() => {
    document.title = 'Graph — VIOS';
  }, []);

  // ── expansion ────────────────────────────────────────────────────────────
  const grow = useCallback(
    async (id: string, replaceAll = false) => {
      setPending((n) => n + 1);
      try {
        const got = await expandNode(id, fan);
        if (!got.ok) return;
        const next: Picture = { nodes: got.nodes || [], edges: got.edges || [] };
        setPic((prev) => (replaceAll ? next : merge(prev, next)));
        setTruncated(
          got.truncated
            ? { id, shown: (got.nodes?.length || 1) - 1, total: got.total || 0 }
            : null
        );
      } catch {
        // A failed expansion leaves the picture alone; the note in the bar
        // covers the only case worth telling the user about.
      } finally {
        setPending((n) => Math.max(0, n - 1));
      }
    },
    [fan]
  );

  // A `?node=` that this component has not acted on yet means a deep link —
  // from Home's concept cloud, or a pasted URL. Selection also writes the
  // param, so the ref is what keeps that from expanding on every click.
  const handled = useRef<string>('');
  useEffect(() => {
    if (!focus || handled.current === focus) return;
    handled.current = focus;
    setSelected(focus);
    void grow(focus);
  }, [focus, grow]);

  /**
   * The whole exploration as query params, with the defaults dropped.
   *
   * Dropping them matters for the thing this screen is for: a link you paste
   * into a note should read `/graph?node=t:frame_notes.objects|coffee`, not that
   * plus three settings nobody touched. Anything left at its default is absent,
   * and `href` already drops empty strings.
   */
  const paramsNow = (patch: Record<string, unknown> = {}): Record<string, unknown> => {
    const all: Record<string, unknown> = {
      node: selected || '',
      keys: keysParam,
      hubs,
      min: minW,
      fan,
      hide: hideParam,
      ...patch,
    };
    if (Number(all.hubs) === HUBS) all.hubs = '';
    if (!(Number(all.min) > 1)) all.min = '';
    if (Number(all.fan) === FANOUT) all.fan = '';
    return all;
  };

  const select = useCallback(
    (id: string | null) => {
      setSelected(id);
      handled.current = id || '';
      go('graph', { params: paramsNow({ node: id || '' }), replace: true });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [keysParam, hubs, minW, fan, hideParam]
  );

  const setParam = (patch: Record<string, unknown>) =>
    go('graph', { params: paramsNow(patch), replace: true });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (pathFrom) setPathFrom(null);
      else if (selected) select(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected, pathFrom, select]);

  // ── search ───────────────────────────────────────────────────────────────
  const typed = useDebounced(find, 180);
  const found = useFetch((signal) => findNodes(typed, 40, signal), [typed], {
    enabled: typed.trim().length > 1,
  });
  const hits = found.data?.results || [];

  // ── path between two nodes ───────────────────────────────────────────────
  const chain = useFetch(
    (signal) => findPath(pathFrom?.id || '', selected || '', 6, signal),
    [pathFrom?.id, selected],
    { enabled: !!pathFrom && !!selected && pathFrom.id !== selected }
  );
  const chainIds = pathFrom && chain.data?.ok ? chain.data.path || [] : [];
  useEffect(() => {
    if (chain.data?.ok) setPic((prev) => merge(prev, chain.data));
  }, [chain.data]);

  // ── what actually goes on the canvas ─────────────────────────────────────
  const byKind = useMemo(() => {
    const out = new Map<string, number>();
    for (const n of pic.nodes) out.set(n.kind, (out.get(n.kind) || 0) + 1);
    return [...out.entries()].sort((a, b) => b[1] - a[1]);
  }, [pic.nodes]);

  const heaviestEdge = useMemo(
    () => pic.edges.reduce((m, e) => Math.max(m, e.weight || 1), 1),
    [pic.edges]
  );

  const shown = useMemo<Picture>(() => {
    const visible = hidden.size ? pic.nodes.filter((n) => !hidden.has(n.kind)) : pic.nodes;
    const ok = new Set(visible.map((n) => n.id));
    const edges = pic.edges.filter(
      (e) => (e.weight || 1) >= minW && ok.has(e.src) && ok.has(e.dst)
    );
    if (minW <= 1) return { nodes: visible, edges };
    // Raising the evidence floor should remove the dots it disconnected, not
    // leave them floating with no visible reason to be there. The selection is
    // deliberately *not* exempted: the inspector is keyed on the id rather than
    // on presence in the picture, so it stays open and readable either way, and
    // exempting it would put `selected` in this dependency list — which would
    // rebuild the canvas's simulation on every single click.
    const kept = new Set<string>();
    for (const e of edges) {
      kept.add(e.src);
      kept.add(e.dst);
    }
    return { nodes: visible.filter((n) => kept.has(n.id)), edges };
  }, [pic, hidden, minW]);

  const marked = useMemo(() => {
    const s = new Set<string>();
    for (const n of hits) s.add(n.id);
    for (const id of chainIds) s.add(id);
    return s;
  }, [hits, chainIds]);

  const counts = base.data?.counts;
  const buildState = base.data?.status;

  const doRebuild = async () => {
    setRebuilding('busy');
    setRebuilt(null);
    try {
      const out = (await rebuildGraph()) as {
        ok?: boolean;
        nodes?: number;
        edges?: number;
        seconds?: number;
        note?: string;
      };
      setRebuilt(
        out.ok
          ? `rebuilt — ${fmtCount(out.nodes)} nodes, ${fmtCount(out.edges)} links in ${
              out.seconds ?? '?'
            }s`
          : out.note || 'rebuild failed'
      );
      base.reload();
    } catch (e) {
      setRebuilt(String((e as Error)?.message || e));
    } finally {
      setRebuilding('');
    }
  };

  const empty = !base.loading && !pic.nodes.length;

  return (
    <div className="view view-split">
      <div className="view-bar">
        <div className="search-box-wrap">
          <SearchIcon size={13} className="sbw-icon" />
          <input
            className="input-text search-box"
            value={find}
            onChange={(e) => setFind(e.target.value)}
            placeholder="find a creator, an object, a hashtag…"
            spellCheck={false}
            aria-label="Find a node"
          />
          {find && (
            <button className="btn-icon sbw-clear" onClick={() => setFind('')} title="clear">
              <X size={12} />
            </button>
          )}
        </div>

        <label className="dens" title="how many connections an expansion pulls in at once, heaviest first">
          <Link2 size={12} /> fan-out
          <input
            type="range"
            min={10}
            max={200}
            step={10}
            value={fan}
            onChange={(e) => setParam({ fan: Number(e.target.value) })}
          />
          <span className="dens-n">{fan}</span>
        </label>

        <label
          className="dens"
          title="hide links asserted by fewer rows than this — 1 shows everything"
        >
          evidence ≥
          <input
            type="range"
            min={0}
            max={Math.max(2, Math.min(20, heaviestEdge))}
            step={1}
            value={minW}
            onChange={(e) => setParam({ min: Number(e.target.value) })}
          />
          <span className="dens-n">{minW || 1}</span>
        </label>

        <span className="spacer" />

        <span className="view-sub" aria-live="polite">
          {fmtCount(shown.nodes.length)} of {fmtCount(counts?.nodes)} nodes ·{' '}
          {fmtCount(shown.edges.length)} of {fmtCount(counts?.edges)} links
          {pending ? ' · expanding…' : ''}
          {base.loading && !base.first ? ' · …' : ''}
        </span>

        {rebuilding === 'ask' ? (
          <>
            <button className="btn btn-danger" onClick={doRebuild}>
              re-read every table
            </button>
            <button className="btn btn-ghost" onClick={() => setRebuilding('')}>
              no
            </button>
          </>
        ) : (
          <button
            className="btn btn-ghost"
            onClick={() => setRebuilding('ask')}
            disabled={rebuilding === 'busy'}
            title="scan every table again and rebuild the node and link tables. Every saved plan is derived from this graph, so they are discarded too."
          >
            <RefreshCw size={12} className={rebuilding === 'busy' ? 'spin' : undefined} /> rebuild
          </button>
        )}
      </div>

      <div className="split">
        <aside className="rail rail-left">
          {keyList.length > 0 && (
            <div className="rail-block">
              <div className="rail-h">from your selection</div>
              <div className="rail-note">
                Built from {plural(keyList.length, 'reel')} you picked, and what they have in
                common. <a href={href('graph')}>show the whole archive instead</a>
              </div>
            </div>
          )}

          {typed.trim().length > 1 && (
            <div className="rail-block">
              <div className="rail-h">
                matches
                <span className="rail-n">{fmtCount(hits.length)}</span>
              </div>
              {found.loading && found.first ? (
                <div className="rail-note">looking…</div>
              ) : hits.length ? (
                <div className="find-list">
                  {hits.map((n) => (
                    <button
                      key={n.id}
                      className={`find-row${n.id === selected ? ' is-active' : ''}`}
                      onClick={() => {
                        select(n.id);
                        if (!pic.nodes.some((x) => x.id === n.id)) void grow(n.id);
                      }}
                      title={`${nodeTypeLabel(n)} — ${nodeNote(n)}`}
                    >
                      <span className="find-dot" style={{ background: nodeCss(n) }} />
                      <span className="find-l">{n.label}</span>
                      <span className="find-w">{fmtCompact(n.weight)}</span>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="rail-note">nothing with that in its name.</div>
              )}
            </div>
          )}

          <div className="rail-block">
            <div className="rail-h">what is on screen</div>
            <div className="kind-list">
              {byKind.map(([kind, n]) => {
                const off = hidden.has(kind);
                const props = new Set(
                  pic.nodes.filter((x) => x.kind === kind).slice(0, 400).map(nodeProp)
                );
                return (
                  <button
                    key={kind}
                    className={`kind-row${off ? ' is-off' : ''}`}
                    onClick={() =>
                      setParam({
                        hide: (off
                          ? [...hidden].filter((k) => k !== kind)
                          : [...hidden, kind]
                        ).join(','),
                      })
                    }
                    title={`${nodeNote({ kind })} — click to ${off ? 'show' : 'hide'}`}
                    aria-pressed={!off}
                  >
                    <span className="kind-sw">
                      {[...props].slice(0, 5).map((prop) => (
                        <i key={prop} style={{ background: `var(${prop})` }} />
                      ))}
                    </span>
                    <span className="kind-l">{kind}</span>
                    <span className="kind-n">{fmtCount(n)}</span>
                  </button>
                );
              })}
            </div>
            <div className="rail-note">
              A colour is <em>who said it</em>, never what it is. Values mined out of a text
              column wear their channel's hue; reels, platform rows and hashtags are deliberately
              colourless, because nobody checked which observer put the <code>#</code> there.
            </div>
          </div>

          <div className="rail-block">
            <div className="rail-h">the whole graph</div>
            <dl className="kv">
              <dt>nodes</dt>
              <dd>{fmtCount(counts?.nodes)}</dd>
              <dt>links</dt>
              <dd>{fmtCount(counts?.edges)}</dd>
              {Object.entries(counts?.kinds || {}).map(([k, n]) => (
                <Fragment key={k}>
                  <dt>{k}</dt>
                  <dd>{fmtCount(n)}</dd>
                </Fragment>
              ))}
              {buildState?.phase && (
                <>
                  <dt>last build</dt>
                  <dd>{buildState.detail || buildState.phase}</dd>
                </>
              )}
            </dl>
            {rebuilt && <div className="rail-note">{rebuilt}</div>}
          </div>
        </aside>

        <div className="split-main gmain">
          {base.error ? (
            <div className="state-box err">
              <div className="head">Could not read the graph</div>
              <div>{base.error}</div>
            </div>
          ) : empty ? (
            <div className="state-box">
              <div className="head">
                {base.data?.note || 'No relationships found in this database'}
              </div>
              <div>
                The graph is built from the tables you already have — foreign keys become
                relationships, and values that repeat across reels become shared nodes. If the
                archive has just arrived, build it once: <strong>rebuild</strong>, top right.
              </div>
            </div>
          ) : (
            <GraphCanvas
              nodes={shown.nodes}
              edges={shown.edges}
              selected={selected}
              onSelect={select}
              onExpand={(id) => void grow(id)}
              marked={marked}
            />
          )}

          {truncated && (
            <div className="gnote">
              Showed the {fmtCount(truncated.shown)} heaviest of {fmtCount(truncated.total)}{' '}
              connections. Raise the fan-out to pull in more.
            </div>
          )}

          {pathFrom && (
            <div className="gpath">
              <RouteIcon size={13} />
              <span>
                chain from <b style={{ color: nodeCss(pathFrom) }}>{pathFrom.label}</b>
              </span>
              {!selected || selected === pathFrom.id ? (
                <span className="dim">— now click the other end</span>
              ) : chain.loading ? (
                <span className="dim">— looking…</span>
              ) : chain.data?.ok ? (
                <span className="gpath-chain">
                  {(chain.data.nodes || []).map((n, i) => (
                    <span key={n.id}>
                      {i > 0 && <ChevronRight size={11} />}
                      <button className="link" onClick={() => select(n.id)} style={{ color: nodeCss(n) }}>
                        {n.label}
                      </button>
                    </span>
                  ))}
                </span>
              ) : (
                <span className="dim">— {chain.data?.note || 'no connection found'}</span>
              )}
              <button className="btn-icon" onClick={() => setPathFrom(null)} title="stop">
                <X size={12} />
              </button>
            </div>
          )}
        </div>

        {selected && (
          <Inspector
            id={selected}
            edges={pic.edges}
            nodes={pic.nodes}
            onSelect={select}
            onExpand={(id) => void grow(id)}
            onOnly={(id) => void grow(id, true)}
            onPathFrom={setPathFrom}
            onClose={() => select(null)}
          />
        )}
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// THE INSPECTOR
// ══════════════════════════════════════════════════════════════════════════

interface InspectorProps {
  id: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  onSelect: (id: string) => void;
  onExpand: (id: string) => void;
  onOnly: (id: string) => void;
  onPathFrom: (n: GraphNode) => void;
  onClose: () => void;
}

function Inspector({
  id,
  nodes,
  edges,
  onSelect,
  onExpand,
  onOnly,
  onPathFrom,
  onClose,
}: InspectorProps) {
  const detail = useFetch((signal) => getNode(id, EDGE_ROWS, signal), [id]);
  const [edgeSel, setEdgeSel] = useState<{ src: string; dst: string; rel: string } | null>(null);
  const [allEdges, setAllEdges] = useState(false);

  useEffect(() => {
    setEdgeSel(null);
    setAllEdges(false);
  }, [id]);

  const label = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const node = detail.data?.ok ? detail.data.node : label.get(id);

  // `meta` is `Record<string, unknown>` by design — each kind carries different
  // keys — so it is read through explicit checks rather than casts at the call
  // site. `videos` is on tag nodes, `source` on tag nodes the builder could
  // attribute; neither is guaranteed.
  const meta = node?.meta || {};
  const inReels = typeof meta.videos === 'number' ? meta.videos : null;
  const observer = typeof meta.source === 'string' ? meta.source : '';

  const mine = useMemo(
    () =>
      edges
        .filter((e) => e.src === id || e.dst === id)
        .sort((a, b) => (b.weight || 1) - (a.weight || 1)),
    [edges, id]
  );

  // A hashtag node records no column, but every edge to it does — so the honest
  // answer to "where did this come from" is per-link, and it lives here.
  const fromColumns = useMemo(() => {
    const out = new Set<string>();
    for (const e of mine) {
      const parts = (e.ref || '').split('|');
      if (parts.length >= 2) out.add(`${parts[0]}.${parts[1]}`);
    }
    return [...out];
  }, [mine]);

  const videos = detail.data?.videos || [];
  const vkey = detail.data?.video_key;
  const shownEdges = allEdges ? mine : mine.slice(0, 24);

  return (
    <aside className="rail rail-right gins">
      <div className="gins-head">
        <span className="gins-dot" style={{ background: node ? nodeCss(node) : 'var(--k-other)' }} />
        <div className="gins-t">
          <div className="gins-l">{node?.label || id}</div>
          <div className="gins-k">
            {node ? `${nodeTypeLabel(node)} — ${nodeNote(node)}` : 'loading…'}
          </div>
        </div>
        <button className="btn-icon" onClick={onClose} title="close (Esc)">
          <X size={13} />
        </button>
      </div>

      <div className="gins-acts">
        <button className="btn btn-ghost" onClick={() => onExpand(id)} title="pull its neighbours into the picture">
          <ChevronDown size={12} /> expand
        </button>
        <button className="btn btn-ghost" onClick={() => onOnly(id)} title="throw the rest away and start again from here">
          <Focus size={12} /> only this
        </button>
        {node && (
          <button className="btn btn-ghost" onClick={() => onPathFrom(node)} title="find the chain of relationships from here to another node">
            <RouteIcon size={12} /> chain from here
          </button>
        )}
        {vkey && (
          <a className="btn" href={href('watch', { key: vkey })}>
            <Play size={12} /> play
          </a>
        )}
      </div>

      {vkey && (
        <a className="gins-poster" href={href('watch', { key: vkey })}>
          <img src={posterUrl(vkey, 360)} alt="" loading="lazy" decoding="async" />
        </a>
      )}

      <dl className="kv">
        <dt>connects</dt>
        <dd title="summed weight of every link on this node — how much of the archive it touches">
          {fmtCompact(node?.weight ?? 0)}
        </dd>
        {node?.sub && (
          <>
            <dt>{node.kind === 'dim' ? 'table' : node.kind === 'tag' ? 'column' : 'group'}</dt>
            <dd>
              {node.kind === 'dim' || node.kind === 'tag' ? (
                <a className="kv-link" href={href('data', { params: { table: String(node.sub) } })}>
                  {node.sub}
                </a>
              ) : (
                node.sub
              )}
            </dd>
          </>
        )}
        {typeof inReels === 'number' && (
          <>
            <dt>in reels</dt>
            <dd>{fmtCount(inReels)}</dd>
          </>
        )}
        {observer && (
          <>
            <dt>observer</dt>
            <dd>{observer}</dd>
          </>
        )}
        {fromColumns.length > 0 && node?.kind === 'hashtag' && (
          <>
            <dt>found in</dt>
            <dd>{fromColumns.join(', ')}</dd>
          </>
        )}
        <dt>id</dt>
        <dd>
          <code>{id}</code>
        </dd>
      </dl>

      {detail.data && !detail.data.ok && (
        <div className="rail-note">
          {detail.data.note || 'the graph has no record of this node — rebuild it?'}
        </div>
      )}

      {(detail.data?.records || []).length > 0 && (
        <div className="rail-block">
          <div className="rail-h">the rows behind it</div>
          {(detail.data?.records || []).map((set) => (
            <Rows key={set.table} set={set} />
          ))}
        </div>
      )}

      {mine.length > 0 && (
        <div className="rail-block">
          <div className="rail-h">
            links
            <span className="rail-n">{fmtCount(mine.length)} on screen</span>
          </div>
          <div className="elist">
            {shownEdges.map((e) => {
              const otherId = e.src === id ? e.dst : e.src;
              const other = label.get(otherId);
              const open =
                edgeSel && edgeSel.src === e.src && edgeSel.dst === e.dst && edgeSel.rel === e.rel;
              return (
                <div className={`erow${open ? ' is-open' : ''}`} key={ekey(e)}>
                  <button
                    className="erow-h"
                    onClick={() => setEdgeSel(open ? null : { src: e.src, dst: e.dst, rel: e.rel })}
                    title={`why: ${e.ref || 'no reference stored'}`}
                  >
                    <span className="erow-rel">{e.rel}</span>
                    <span className="erow-l" style={{ color: other ? nodeCss(other) : undefined }}>
                      {other?.label || otherId}
                    </span>
                    <span className="erow-w" title="how many rows assert this link">
                      ×{fmtCompact(e.weight || 1)}
                    </span>
                  </button>
                  <button
                    className="btn-icon erow-go"
                    onClick={() => onSelect(otherId)}
                    title="inspect that end"
                  >
                    <ArrowRight size={11} />
                  </button>
                  {open && <EdgeWhy src={e.src} dst={e.dst} rel={e.rel} ref_={e.ref || ''} />}
                </div>
              );
            })}
          </div>
          {mine.length > shownEdges.length && (
            <button className="btn btn-ghost" onClick={() => setAllEdges(true)}>
              show the other {fmtCount(mine.length - shownEdges.length)}
            </button>
          )}
        </div>
      )}

      {videos.length > 0 && (
        <div className="rail-block">
          <div className="rail-h">
            reels it reaches
            <span className="rail-n">{fmtCount(videos.length)}</span>
          </div>
          <div className="vlist">
            {videos.map((v) => (
              <a className="vrow" key={v.video_key} href={href('watch', { key: v.video_key })}>
                <img src={posterUrl(v.video_key, 160)} alt="" loading="lazy" decoding="async" />
                <span className="vrow-t">
                  <span className="vrow-title">{v.title || v.video_key}</span>
                  <span className="vrow-sub">
                    {v.creator || 'unattributed'}
                    {v.duration ? ` · ${fmtDur(v.duration)}` : ''}
                    {v.moment_count ? ` · ${plural(v.moment_count, 'claim')}` : ''}
                  </span>
                </span>
              </a>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}

/** Why an edge exists — the rows the stored `ref` points at. */
function EdgeWhy({ src, dst, rel, ref_ }: { src: string; dst: string; rel: string; ref_: string }) {
  const got = useFetch((signal) => getEdge(src, dst, rel, 20, signal), [src, dst, rel]);
  const parts = ref_.split('|');
  return (
    <div className="ewhy">
      <div className="ewhy-ref">
        {parts.length >= 2 ? (
          <>
            asserted by <code>{parts[0]}.{parts[1]}</code>
            {parts[2] ? (
              <>
                {' '}
                = <code>{clip(parts[2], 60)}</code>
              </>
            ) : null}
          </>
        ) : (
          'no reference stored for this link'
        )}
      </div>
      {got.loading && got.first ? (
        <div className="rail-note">reading the rows…</div>
      ) : got.error ? (
        <div className="rail-note">{got.error}</div>
      ) : (got.data?.records || []).length ? (
        (got.data?.records || []).map((set) => <Rows key={set.table} set={set} compact />)
      ) : (
        <div className="rail-note">
          The link is stored, but the query rebuilt from its reference matched no rows — the table
          it came from has changed since the graph was built.
        </div>
      )}
    </div>
  );
}

/**
 * One table's rows, as they are.
 *
 * Empty columns are folded away rather than printed: a `moments` row has thirty
 * columns and a handful of them carry the answer, and a wall of `null` is what
 * made v1's provenance panel unreadable. The count of what was folded is shown,
 * so nothing is hidden silently. Every value is a link into the provenance
 * drawer — the same drawer the Data tab opens — because the whole promise of
 * this screen is that a line on a canvas bottoms out in a row.
 */
function Rows({ set, compact }: { set: RecordSet; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const cap = compact ? 2 : 4;
  const rows = open ? set.rows : set.rows.slice(0, cap);
  return (
    <div className="rset">
      <div className="rset-h">
        <a className="kv-link" href={href('data', { params: { table: set.table } })}>
          {set.table}
        </a>
        <span className="rset-n">{plural(set.rows.length, 'row')}</span>
      </div>
      {rows.map((row, i) => {
        const filled = Object.entries(row).filter(
          ([, v]) => v !== null && v !== undefined && String(v).trim() !== ''
        );
        const empties = Object.keys(row).length - filled.length;
        return (
          <dl className="kv rset-row" key={i}>
            {filled.map(([k, v]) => (
              <Fragment key={k}>
                <dt>{k}</dt>
                <dd>
                  <button
                    className="cell-btn"
                    onClick={() => store.openDrill({ table: set.table, column: k, value: String(v) })}
                    title="where this value came from, and who else says it"
                  >
                    {clip(String(v), 220)}
                  </button>
                </dd>
              </Fragment>
            ))}
            {empties > 0 && (
              <>
                <dt className="dim">empty</dt>
                <dd className="dim">{plural(empties, 'column')} with nothing in them</dd>
              </>
            )}
          </dl>
        );
      })}
      {set.rows.length > rows.length && (
        <button className="btn btn-ghost" onClick={() => setOpen(true)}>
          show all {fmtCount(set.rows.length)}
        </button>
      )}
    </div>
  );
}

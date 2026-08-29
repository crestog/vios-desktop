/**
 * views/Studio.tsx — the archive read as craft.
 *
 * Search asks what is in the archive. Graph asks what is connected. Roadmap asks
 * what order to watch it in. This screen asks the question the other three leave
 * out: **how were these made, and what do the ones that work have in common** —
 * so the next reel can be built from measurement instead of memory.
 *
 * Three modes over one scope, and the scope is the whole point:
 *
 *   - **Patterns** — distributions across many reels. How long, how fast, how
 *     much of it is talking, which phrases carry the opening.
 *   - **Reel** — one reel taken apart: cut rhythm, a channel-by-time heatmap,
 *     and the change points where it stops doing one thing and starts another.
 *   - **Script** — the same scope as a beat sheet, every number a median of real
 *     reels and every line citing the reel and timecode it came from.
 *
 * Four decisions worth reading before editing:
 *
 *   - **Null is not zero, ever.** `studio.py` returns `null` for every rate
 *     whose denominator is missing — a reel with no detected shots is *absent*
 *     from the cut-rate distribution rather than counted as having a cut rate of
 *     zero. A `?? 0` anywhere in this file would put that lie back on screen, so
 *     nullable numbers go through {@link n} and render as an em dash.
 *
 *   - **Every rate is shown with its denominator.** "62% open on a caption" is
 *     meaningless without "of 34 reels", and the server sends the `n` beside
 *     every statistic precisely so this file can print it.
 *
 *   - **No prose is generated, here or on the server.** The beat sheet reports
 *     medians and quotes real moments. It never writes a line for you, because
 *     that is the one part of this a measurement cannot honestly supply.
 *
 *   - **The scope lives in the URL.** `?goal=`, `?creator=`, `?category=`,
 *     `?mode=` and `?key=` — so any view of this screen is a link, Back works,
 *     and a pasted URL reproduces the exact numbers someone else was reading.
 */

import { Fragment, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  Clapperboard,
  Copy,
  Film,
  Layers,
  ListOrdered,
  Quote,
  Scissors,
  SlidersHorizontal,
  Sparkles,
  Target,
  Timer,
  X,
} from 'lucide-react';
import type {
  Archetype,
  Band,
  Beat,
  DeconstructResponse,
  PatternsResponse,
  Phrase,
  ReelFeatures,
  ScriptResponse,
  Stats,
} from '../types';
import type { ViewProps } from '../lib/router';
import { go, href, watch } from '../lib/router';
import { getDeconstruct, getPatterns, getScriptDraft } from '../lib/api';
import { useDebounced, useFetch } from '../lib/useFetch';
import type { FetchState } from '../lib/useFetch';
import { CHANNEL_MEANING, channelOf, channelVar, chipClass } from '../lib/channels';
import { clip, fmtCount, fmtDur, fmtPct, fmtT, plural } from '../lib/format';

type Mode = 'patterns' | 'reel' | 'script';

const MODES: Array<{ id: Mode; label: string; hint: string }> = [
  { id: 'patterns', label: 'Patterns', hint: 'distributions across the scope' },
  { id: 'reel', label: 'Reel', hint: 'one reel taken apart' },
  { id: 'script', label: 'Script', hint: 'the scope as a beat sheet' },
];

/**
 * The seven measures, in the order a person asks about them.
 *
 * Each carries its own formatter rather than a unit string, because the seven
 * are not the same kind of number: a share belongs on screen as `62%`, a rhythm
 * index as `0.71`, and a runtime as `14.2s`. One shared `${v}${unit}` would
 * print `0.62×` for the first, which is a measurement nobody asked for.
 */
const MEASURES: Array<{
  id: keyof PatternsResponse['measures'];
  label: string;
  why: string;
  fmt: (v: number | null | undefined) => string;
}> = [
  { id: 'duration', label: 'runtime', why: 'how long these reels actually are', fmt: (v) => n(v, 1, 's') },
  {
    id: 'cuts_per_min',
    label: 'cuts / min',
    why: 'cut rate, over reels with detected shots',
    fmt: (v) => n(v, 1),
  },
  { id: 'shot_len', label: 'shot length', why: 'mean seconds between cuts, per reel', fmt: (v) => n(v, 2, 's') },
  {
    id: 'regularity',
    label: 'rhythm',
    why: '1 − CV of shot length: 1 is a metronome, 0 is one long hold and a burst',
    fmt: (v) => n(v, 2),
  },
  {
    id: 'moments_per_min',
    label: 'evidence / min',
    why: 'how densely the pipeline described it',
    fmt: (v) => n(v, 1),
  },
  { id: 'words_per_s', label: 'words / s', why: 'speaking rate across the whole runtime', fmt: (v) => n(v, 2) },
  { id: 'speech_share', label: 'talking', why: 'share of runtime covered by speech', fmt: (v) => pct(v) },
];

/** A nullable number, rendered honestly. Null means "not measured", not zero. */
function n(v: number | null | undefined, digits = 1, suffix = ''): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${Number(v).toFixed(digits).replace(/\.0+$/, '')}${suffix}`;
}

/** A 0–1 rate as a percentage, or an em dash. Never `0%` for "unknown". */
function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

/** `n=14` in the one place it belongs: right next to the number it qualifies. */
function ofN(count: number): string {
  return `of ${fmtCount(count)}`;
}

export default function StudioView({ route }: ViewProps) {
  const p = route.params;
  const key = p.get('key') || '';
  const goal = p.get('goal') || '';
  const creator = p.get('creator') || '';
  const category = p.get('category') || '';
  // A `?key=` in the URL means somebody linked to a reel, so that is the mode
  // regardless of what `?mode=` says — a link to a reel that opened on the
  // distributions would be a link that did not work.
  const mode: Mode = key ? 'reel' : ((p.get('mode') as Mode) || 'patterns');

  const [goalInput, setGoalInput] = useState(goal);
  useEffect(() => setGoalInput(goal), [goal]);
  const typedGoal = useDebounced(goalInput, 250);

  const scopeArgs = useMemo(() => ({ goal, creator, category }), [goal, creator, category]);

  const paramsNow = (patch: Record<string, unknown> = {}): Record<string, unknown> => ({
    mode: mode === 'patterns' ? '' : mode,
    key,
    goal,
    creator,
    category,
    ...patch,
  });
  const setParam = (patch: Record<string, unknown>, replace = false) =>
    go('studio', { params: paramsNow(patch), replace });

  useEffect(() => {
    if (typedGoal === goal) return;
    setParam({ goal: typedGoal }, true);
  }, [typedGoal]); // eslint-disable-line react-hooks/exhaustive-deps

  // Patterns is fetched in every mode: it is the spine of the screen — it
  // resolves the scope, and its `reel_rows` are the rail's reel picker. The
  // server caches it against a fingerprint of the three tables it reads, so
  // flipping between modes costs one dictionary lookup, not one scope load.
  const pat = useFetch((s) => getPatterns(scopeArgs, s), [goal, creator, category]);
  const one = useFetch((s) => getDeconstruct(key, s), [key], { enabled: !!key });
  const script = useFetch((s) => getScriptDraft(scopeArgs, s), [goal, creator, category], {
    enabled: mode === 'script',
  });

  const scope = pat.data?.scope;
  const reels = pat.data?.reels ?? 0;
  const rows = pat.data?.reel_rows ?? [];

  return (
    <div className="view view-split">
      <div className="view-bar">
        <Clapperboard size={14} className="dim" />
        <strong>
          {mode === 'reel'
            ? one.data?.video.title || (key ? 'Reel' : 'Pick a reel')
            : goal
              ? `How “${goal}” is made`
              : creator || category
                ? `How ${creator || category} makes them`
                : 'How this archive is made'}
        </strong>
        {mode !== 'reel' && pat.data && (
          <span className="dim">
            {fmtCount(reels)} reel{reels === 1 ? '' : 's'}
            {scope && scope.archive > reels ? ` ${ofN(scope.archive)}` : ''}
          </span>
        )}
        {mode === 'reel' && one.data && (
          <span className="dim">
            {fmtDur(one.data.duration)} · {plural(one.data.moments, 'moment')} ·{' '}
            {one.data.pacing.shots ? plural(one.data.pacing.shots, 'shot') : 'no shots detected'}
          </span>
        )}

        <span className="spacer" />

        <div className="search-box-wrap">
          <Target size={13} className="sbw-icon" />
          <input
            className="input-text search-box"
            value={goalInput}
            onChange={(e) => setGoalInput(e.target.value)}
            placeholder="narrow the scope — “cooking reels”, “hook”, a creator’s subject"
            spellCheck={false}
            aria-label="Narrow the scope"
          />
          {goalInput && (
            <button className="btn-icon sbw-clear" onClick={() => setGoalInput('')} title="whole archive">
              <X size={12} />
            </button>
          )}
        </div>

        <div className="segmented" role="tablist" aria-label="Studio mode">
          {MODES.map((m) => (
            <button
              key={m.id}
              role="tab"
              aria-selected={mode === m.id}
              className={mode === m.id ? 'on' : undefined}
              title={m.hint}
              // Leaving Reel mode has to drop `?key=`, or the mode would snap
              // straight back — `key` wins over `mode` by design above.
              onClick={() => setParam({ mode: m.id, key: m.id === 'reel' ? key : '' })}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      <div className="split">
        <aside className="rail rail-left" aria-label="Scope and reels">
          {/* `.rail-body` is the scroll container the rail already provides —
              `.rail` itself is `overflow: hidden`, so blocks dropped straight
              into it would be clipped rather than scrolled. */}
          <div className="rail-body">
            <ScopeBlock state={pat} creator={creator} category={category} onClear={setParam} />
            <ReelPicker rows={rows} active={key} paramsNow={paramsNow} loading={pat.loading && pat.first} />
            <MethodBlock mode={mode} data={pat.data} />
          </div>
        </aside>

        <div className="split-main stu-main">
          {mode === 'reel' ? (
            <ReelPane state={one} hasKey={!!key} rows={rows} paramsNow={paramsNow} />
          ) : mode === 'script' ? (
            <ScriptPane state={script} />
          ) : (
            <PatternsPane state={pat} paramsNow={paramsNow} />
          )}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared pieces
// ─────────────────────────────────────────────────────────────────────────────

/**
 * The three things that are not data, in the one order that reads correctly:
 * an error replaces the pane, a first load shows bones, and an empty result
 * says so in a sentence. A *reload* shows none of them — `useFetch` keeps the
 * previous data on screen, so flipping the scope must not blink.
 */
function PaneState({
  state,
  empty,
  children,
}: {
  state: FetchState<unknown>;
  empty?: React.ReactNode;
  children: React.ReactNode;
}) {
  if (state.error) {
    return (
      <div className="state-box">
        <strong>{state.error}</strong>
        <button className="btn btn-sm" onClick={state.reload}>
          try again
        </button>
      </div>
    );
  }
  if (!state.data && state.loading) {
    return (
      <div className="stu-skels">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="skel stu-skel" />
        ))}
      </div>
    );
  }
  if (!state.data) return <div className="state-box">{empty || 'Nothing to show.'}</div>;
  return <>{children}</>;
}

/** Sentences the server wrote about its own thin spots. Never invented here. */
function Notes({ notes }: { notes: string[] }) {
  if (!notes || !notes.length) return null;
  return (
    <div className="stu-notes">
      {notes.map((t, i) => (
        <div key={i} className="view-hint">
          {t}
        </div>
      ))}
    </div>
  );
}

/**
 * A channel name in its own colour, with what it means on hover.
 *
 * The label is the *literal* source string, not the resolved channel: a future
 * pass that writes `speech_hi` should read as `speech_hi` in sky blue, because
 * printing the resolved name would quietly claim the archive has a channel it
 * does not have. Colour resolves, text does not.
 */
function Chan({ source, n: count }: { source: string; n?: number }) {
  const c = channelOf(source);
  return (
    <span className={chipClass(source)} title={`${c} — ${CHANNEL_MEANING[c]}`}>
      {source || c}
      {count !== undefined ? ` ${fmtCount(count)}` : ''}
    </span>
  );
}
/**
 * A distribution as a bar: the p10–p90 band, with the median marked.
 *
 * Deliberately not a histogram. A histogram of 34 reels is mostly empty bins,
 * and the question this screen answers is "what range am I aiming at" — which
 * is two edges and a middle. The bar's own domain is the scope's `min…max`, so
 * a band that fills the track means the whole scope agrees and a narrow one in
 * a wide track means the median is a real target rather than an average of two
 * different kinds of reel.
 */
function DistBar({ s, fmt }: { s: Stats; fmt: (v: number | null | undefined) => string }) {
  const { min, max, p10, p90, median } = s;
  const nums = [min, max, p10, p90, median];
  if (s.n < 2 || nums.some((v) => v === null || v === undefined || !Number.isFinite(v))) return null;
  const lo = min as number;
  const hi = max as number;
  const span = hi - lo;
  // A zero span means every reel in the scope measured identically. Dividing by
  // it would paint `NaN%`, so the band takes the whole track — which is the
  // truthful picture of that case anyway.
  const at = (v: number) => (span > 0 ? ((v - lo) / span) * 100 : 0);
  const left = span > 0 ? at(p10 as number) : 0;
  const width = span > 0 ? Math.max(at(p90 as number) - left, 1.5) : 100;
  return (
    <div
      className="stu-dbar"
      title={`min ${fmt(min)} · p10 ${fmt(p10)} · median ${fmt(median)} · p90 ${fmt(p90)} · max ${fmt(max)}`}
    >
      <span className="stu-dbar-band" style={{ left: `${left}%`, width: `${width}%` }} />
      <span className="stu-dbar-tick" style={{ left: `${at(median as number)}%` }} />
    </div>
  );
}

/** One measure: name, median, denominator, band, and the numbers under it. */
function DistRow({ m, s }: { m: (typeof MEASURES)[number]; s: Stats }) {
  return (
    <div className="stu-drow">
      <div className="stu-drow-h">
        <span className="stu-drow-l" title={m.why}>
          {m.label}
        </span>
        <span className="stu-drow-v">{m.fmt(s.median)}</span>
        <span className="stu-drow-n">{ofN(s.n)}</span>
      </div>
      <DistBar s={s} fmt={m.fmt} />
      <div className="stu-drow-f">
        {s.n >= 2
          ? `p10 ${m.fmt(s.p10)} · p90 ${m.fmt(s.p90)} · mean ${m.fmt(s.mean)} · CV ${n(s.cv, 2)}`
          : m.why}
      </div>
    </div>
  );
}
/**
 * A rate with the two numbers that make it meaningful: the count and the
 * denominator. `62%` on its own is a claim; `21 of 34` is a measurement.
 */
function RateRow({
  source,
  count,
  rate,
  of,
}: {
  source: string;
  count: number;
  rate: number | null;
  of: number;
}) {
  return (
    <div className="stu-rate">
      <Chan source={source} />
      <span className="stu-rate-track">
        <span
          className="stu-rate-fill"
          style={{
            width: `${Math.max(0, Math.min(1, rate ?? 0)) * 100}%`,
            background: channelVar(source),
          }}
        />
      </span>
      <span className="stu-rate-v">{pct(rate)}</span>
      <span className="stu-rate-n">
        {fmtCount(count)} {ofN(of)}
      </span>
    </div>
  );
}

/**
 * The log-odds ranking, as bars.
 *
 * The bar length is |z| relative to the strongest term in the list, so it reads
 * as "how much more this scope's language is this word than everyone else's".
 * The hover carries the whole test: raw counts both sides, rate per 1,000 words
 * both sides, the log-odds and the z. A word that appears three times in one
 * reel cannot outrank a word that appears eighty times across thirty — the
 * prior in `studio._lift` shrinks it — and the tooltip is where you can check
 * that for yourself.
 */
function PhraseList({
  phrases,
  basis,
}: {
  phrases: Phrase[];
  basis?: { hook_terms: number; rest_terms: number };
}) {
  if (!phrases.length) return <div className="dim">Not enough text in this scope to compare yet.</div>;
  const max = Math.max(...phrases.map((p) => Math.abs(p.z))) || 1;
  return (
    <div className="stu-phrases">
      {phrases.map((p) => (
        <div
          key={p.term}
          className="stu-phrase"
          title={
            `${fmtCount(p.n_in)}× here vs ${fmtCount(p.n_out)}× elsewhere · ` +
            `${p.per_k_in.toFixed(1)} vs ${p.per_k_out.toFixed(1)} per 1k words · ` +
            `log-odds ${p.log_odds.toFixed(2)}, z ${p.z.toFixed(2)}`
          }
        >
          <span className="stu-phrase-z" style={{ width: `${(Math.abs(p.z) / max) * 100}%` }} />
          <span className="stu-phrase-t">{p.term}</span>
          <span className="stu-phrase-n">{p.z.toFixed(1)}</span>
        </div>
      ))}
      {basis && (
        <div className="stu-basis">
          {fmtCount(basis.hook_terms)} words here, {fmtCount(basis.rest_terms)} elsewhere
        </div>
      )}
    </div>
  );
}
// ─────────────────────────────────────────────────────────────────────────────
// The rail: what am I looking at, which reel, and by what method
// ─────────────────────────────────────────────────────────────────────────────

function ScopeBlock({
  state,
  creator,
  category,
  onClear,
}: {
  state: FetchState<PatternsResponse>;
  creator: string;
  category: string;
  onClear: (patch: Record<string, unknown>, replace?: boolean) => void;
}) {
  const d = state.data;
  const scope = d?.scope;
  return (
    <div className="rail-block">
      <div className="rail-h">
        scope
        {d ? <span className="rail-n">{fmtCount(d.reels)}</span> : null}
      </div>
      {state.error ? (
        <div className="rail-note stu-bad">{state.error}</div>
      ) : scope && d ? (
        <>
          {/* The server's own sentence about what it resolved — including any
              fallback it took when a search returned too few hits to plan on.
              Writing our own version here would be a second, quieter answer. */}
          <div className="rail-note">{scope.note}</div>
          {(creator || category) && (
            <div className="stu-filters">
              {creator && (
                <button className="stu-filter" onClick={() => onClear({ creator: '' })} title="clear creator">
                  {creator} <X size={10} />
                </button>
              )}
              {category && (
                <button className="stu-filter" onClick={() => onClear({ category: '' })} title="clear category">
                  {category} <X size={10} />
                </button>
              )}
            </div>
          )}
          <dl className="kv stu-kv">
            <dt>in archive</dt>
            <dd>{fmtCount(scope.archive)}</dd>
            <dt>in scope</dt>
            <dd>
              {fmtCount(d.reels)}
              {scope.archive > 0 ? ` · ${fmtPct(d.reels, scope.archive)}` : ''}
            </dd>
          </dl>
        </>
      ) : (
        <div className="rail-note">{state.loading ? 'reading the index…' : 'nothing indexed yet'}</div>
      )}
    </div>
  );
}
/**
 * The reel picker, present in all three modes.
 *
 * It stays in the rail while Patterns and Script are on screen because the
 * distributions are an argument about a *set*, and the reflex the moment a
 * number looks wrong is to go and watch one of the reels behind it. The list is
 * the scope's own `reel_rows` (server-capped at 60), so it can never disagree
 * with the numbers to its right.
 */
function ReelPicker({
  rows,
  active,
  paramsNow,
  loading,
}: {
  rows: ReelFeatures[];
  active: string;
  paramsNow: (patch?: Record<string, unknown>) => Record<string, unknown>;
  loading: boolean;
}) {
  const [q, setQ] = useState('');
  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    if (!t) return rows;
    return rows.filter((r) => `${r.title} ${r.creator} ${r.category}`.toLowerCase().includes(t));
  }, [rows, q]);

  return (
    <div className="rail-block">
      <div className="rail-h">
        reels
        <span className="rail-n">{fmtCount(rows.length)}</span>
      </div>
      {rows.length > 8 && (
        <input
          className="input-text stu-rail-find"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="find in this scope"
          spellCheck={false}
          aria-label="Find a reel in this scope"
        />
      )}
      {loading && !rows.length ? (
        <div className="stu-skels">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="skel stu-skel-row" />
          ))}
        </div>
      ) : !shown.length ? (
        <div className="rail-note">{rows.length ? 'no reel here matches that' : 'no reels in this scope'}</div>
      ) : (
        <div className="row-list">
          {shown.map((r) => (
            <a
              key={r.video_key}
              className={`tbl${r.video_key === active ? ' is-active' : ''}`}
              href={href('studio', { params: paramsNow({ mode: 'reel', key: r.video_key }) })}
              title={`${r.title}${r.creator ? ` — ${r.creator}` : ''}\n${n(r.cuts_per_min, 1)} cuts/min · ${pct(
                r.speech_share
              )} talking · ${plural(r.moments, 'moment')}`}
            >
              <span className="tbl-n">{r.title || r.video_key}</span>
              <span className="tbl-r">{fmtDur(r.duration)}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
/**
 * How the numbers on the right were made.
 *
 * In the rail rather than a footnote because it changes what the numbers mean:
 * "tertiles of this scope" and "fixed thresholds" produce the same three band
 * names and completely different memberships, and a reader who does not know
 * which one they are looking at cannot use either.
 */
function MethodBlock({ mode, data }: { mode: Mode; data: PatternsResponse | null }) {
  const m = data?.method;
  return (
    <div className="rail-block">
      <div className="rail-h">method</div>
      {mode === 'reel' ? (
        <>
          <div className="rail-note">
            Sections come from binary segmentation over 96 equal time bins — the split that most reduces
            within-segment variance, taken recursively while it still explains 6% of what is left, to at
            most six sections of at least four bins each.
          </div>
          <div className="rail-note">
            A section's label names the channels that actually occupy it. It is a measurement, not a
            reading: nothing here decides that a stretch is “the reveal”.
          </div>
        </>
      ) : mode === 'script' ? (
        <>
          <div className="rail-note">
            The five slots are a positional convention — fixed proportions of runtime carrying the
            editorial names. What is measured is what occupies them.
          </div>
          <div className="rail-note">
            Every number is a median across the scope, every quote is a real moment from a real reel, and
            no line is written for you.
          </div>
        </>
      ) : (
        <>
          <div className="rail-note">{m ? `Bands: ${m.bands}.` : 'Bands: tertiles of this scope.'}</div>
          <div className="rail-note">{m ? `Phrases: ${m.phrases}.` : ''}</div>
          <div className="rail-note">{m ? `Hook: ${m.compared}.` : ''}</div>
        </>
      )}
    </div>
  );
}
// ─────────────────────────────────────────────────────────────────────────────
// Patterns — the scope as distributions
// ─────────────────────────────────────────────────────────────────────────────

function PatternsPane({
  state,
  paramsNow,
}: {
  state: FetchState<PatternsResponse>;
  paramsNow: (patch?: Record<string, unknown>) => Record<string, unknown>;
}) {
  const d = state.data;
  return (
    <PaneState
      state={state}
      empty={
        <>
          <strong>Nothing is indexed yet.</strong>
          <span>Studio reads the same index Search does — capture or import a reel and this fills in.</span>
        </>
      }
    >
      {d &&
        (d.reels === 0 ? (
          <div className="state-box">
            <strong>No reels in this scope.</strong>
            <span>{d.scope.note}</span>
          </div>
        ) : (
          <>
            <Notes notes={d.notes} />

            <section>
              <div className="section-h">
                <h2>Shape</h2>
                <span className="count">{plural(d.reels, 'reel')} in scope</span>
              </div>
              <div className="stu-dists">
                {MEASURES.map((m) => (
                  <DistRow key={m.id} m={m} s={d.measures[m.id]} />
                ))}
              </div>
            </section>

            <ChannelTable rows={d.channels} reels={d.reels} />
            <HookAgg hook={d.hook} window={d.method.hook_window} />
            <Archetypes bands={d.bands} list={d.archetypes} paramsNow={paramsNow} />
            <ReelTable rows={d.reel_rows} total={d.reels} paramsNow={paramsNow} />
          </>
        ))}
    </PaneState>
  );
}

/** Which observers were present, how often, and how much runtime they cover. */
function ChannelTable({ rows, reels }: { rows: PatternsResponse['channels']; reels: number }) {
  if (!rows.length) return null;
  return (
    <section>
      <div className="section-h">
        <h2>Evidence channels</h2>
        <span className="count">{rows.length} present in this scope</span>
      </div>
      <div className="stu-ct">
        <div className="stu-ct-h">
          <span>channel</span>
          <span>carried by</span>
          <span>runtime share</span>
          <span>spread of that share</span>
        </div>
        {rows.map((c) => (
          <div key={c.source} className="stu-ct-r">
            <Chan source={c.source} />
            <span className="stu-ct-v">
              {pct(c.rate)} <em>{fmtCount(c.n)} {ofN(reels)}</em>
            </span>
            {/* The denominator changes between these two columns on purpose:
                `rate` is over the whole scope, `share` only over the reels that
                actually carry the channel. Averaging a share across reels that
                never had it would drag every channel toward zero. */}
            <span className="stu-ct-v">
              {pct(c.share.median)} <em>{ofN(c.share.n)}</em>
            </span>
            <DistBar s={c.share} fmt={(v) => pct(v)} />
          </div>
        ))}
      </div>
    </section>
  );
}
/**
 * The opening, aggregated — the one part of a reel that decides whether the
 * rest is watched, so it gets its own section rather than a row in a table.
 *
 * Two questions that look identical and are not: what is on the *first frame*,
 * and what *leads the window*. A reel can open on a silent caption card and be
 * speech-led two seconds later; `opens_with` answers the first, `leads_with`
 * the second, and showing only one of them would answer the wrong one half the
 * time.
 */
function HookAgg({ hook, window: win }: { hook: PatternsResponse['hook']; window: number }) {
  const of = hook.reels;
  if (!of) return null;
  return (
    <section>
      <div className="section-h">
        <h2>The first {n(win, 1)}s</h2>
        <span className="count">of {plural(of, 'reel')} with evidence there</span>
      </div>

      <div className="stu-hook">
        <div className="panel">
          <div className="panel-h">
            <Film size={13} /> on the first frame
          </div>
          {hook.opens_with.length ? (
            hook.opens_with.map((r) => (
              <RateRow key={r.source} source={r.source} count={r.n} rate={r.rate} of={of} />
            ))
          ) : (
            <div className="dim">nothing is timed at t=0 in this scope</div>
          )}
        </div>

        <div className="panel">
          <div className="panel-h">
            <Layers size={13} /> leads the window
          </div>
          {hook.leads_with.length ? (
            hook.leads_with.map((r) => (
              <RateRow key={r.source} source={r.source} count={r.n} rate={r.rate} of={of} />
            ))
          ) : (
            <div className="dim">no channel leads often enough to name</div>
          )}
        </div>

        <div className="panel">
          <div className="panel-h">
            <Timer size={13} /> how it starts
          </div>
          <dl className="kv stu-kv">
            <dt>silent open</dt>
            <dd>
              {pct(hook.silent_open.rate)} <em className="dim">{fmtCount(hook.silent_open.n)} {ofN(of)}</em>
            </dd>
            <dt>first word at</dt>
            <dd>
              {n(hook.first_speech_at.median, 2, 's')} <em className="dim">{ofN(hook.first_speech_at.n)}</em>
            </dd>
            <dt>words spoken</dt>
            <dd>
              {n(hook.words.median, 0)} <em className="dim">{ofN(hook.words.n)}</em>
            </dd>
            <dt>cuts</dt>
            <dd>
              {n(hook.cuts.median, 0)} <em className="dim">{ofN(hook.cuts.n)}</em>
            </dd>
          </dl>
        </div>
      </div>

      <div className="section-h stu-sub-h">
        <h2>What the openings say</h2>
        <span className="count">the first {n(win, 1)}s against the rest of the same reels</span>
      </div>
      <PhraseList phrases={hook.phrases} basis={hook.phrase_basis} />
    </section>
  );
}
/**
 * Cut rate × talking, as a 3×3 of the scope's own tertiles.
 *
 * The grid exists because the two measures interact: 40 cuts a minute over
 * wall-to-wall narration is a completely different reel from 40 cuts over music,
 * and two separate distributions cannot show you that. Reading down a column
 * holds the talking constant and varies the cutting, which is the comparison you
 * want before choosing how to edit.
 *
 * When either measure cannot be cut into thirds — fewer than six reels carry it,
 * or it is too uniform — the grid is replaced by the server's stated reason. An
 * empty 3×3 would imply nine kinds of reel exist and eight are unpopulated.
 */
function Archetypes({
  bands,
  list,
  paramsNow,
}: {
  bands: { pace: Band; talk: Band };
  list: Archetype[];
  paramsNow: (patch?: Record<string, unknown>) => Record<string, unknown>;
}) {
  const { pace, talk } = bands;
  if (!pace.ok || !talk.ok) {
    return (
      <section>
        <div className="section-h">
          <h2>Kinds of reel</h2>
        </div>
        <div className="view-hint">
          {[pace.ok ? '' : `Cut rate: ${pace.why}.`, talk.ok ? '' : `Talking: ${talk.why}.`]
            .filter(Boolean)
            .join(' ')}
        </div>
      </section>
    );
  }
  const cell = new Map(list.map((a) => [`${a.pace}|${a.talk}`, a]));
  const max = Math.max(1, ...list.map((a) => a.n));
  return (
    <section>
      <div className="section-h">
        <h2>Kinds of reel</h2>
        <span className="count">cut rate × talking, in tertiles of this scope</span>
      </div>
      <div className="stu-arch" style={{ gridTemplateColumns: `auto repeat(${talk.names.length}, 1fr)` }}>
        <span />
        {talk.names.map((t) => (
          <span key={t} className="stu-arch-h">
            {t}
          </span>
        ))}
        {pace.names.map((p) => (
          <Fragment key={p}>
            <span className="stu-arch-rh">{p}</span>
            {talk.names.map((t) => {
              const a = cell.get(`${p}|${t}`);
              return (
                <div
                  key={t}
                  className={`stu-arch-c${a ? '' : ' is-empty'}`}
                  style={a ? { background: `rgba(129, 140, 248, ${0.06 + 0.3 * (a.n / max)})` } : undefined}
                >
                  <span className="stu-arch-n">{a ? fmtCount(a.n) : '—'}</span>
                  {a?.examples.map((ex) => (
                    <a
                      key={ex.video_key}
                      className="stu-arch-ex"
                      href={href('studio', { params: paramsNow({ mode: 'reel', key: ex.video_key }) })}
                      title={`${ex.title}\n${n(ex.cuts_per_min, 1)} cuts/min · ${pct(ex.speech_share)} talking`}
                    >
                      {clip(ex.title || ex.video_key, 40)}
                    </a>
                  ))}
                </div>
              );
            })}
          </Fragment>
        ))}
      </div>
      <div className="dim stu-edges">
        slow / steady / fast split at {n(pace.edges[0], 1)} and {n(pace.edges[1], 1)} cuts per minute · quiet /
        mixed / talky at {pct(talk.edges[0])} and {pct(talk.edges[1])} of runtime spoken
      </div>
    </section>
  );
}
type RCol = {
  id: string;
  label: string;
  get: (r: ReelFeatures) => number | null;
  fmt: (r: ReelFeatures) => string;
};

const RCOLS: RCol[] = [
  { id: 'duration', label: 'runtime', get: (r) => r.duration, fmt: (r) => fmtDur(r.duration) },
  { id: 'cuts_per_min', label: 'cuts/min', get: (r) => r.cuts_per_min, fmt: (r) => n(r.cuts_per_min, 1) },
  { id: 'shot_len', label: 'shot', get: (r) => r.shot_len, fmt: (r) => n(r.shot_len, 2, 's') },
  { id: 'regularity', label: 'rhythm', get: (r) => r.regularity, fmt: (r) => n(r.regularity, 2) },
  { id: 'words_per_s', label: 'words/s', get: (r) => r.words_per_s, fmt: (r) => n(r.words_per_s, 2) },
  { id: 'speech_share', label: 'talking', get: (r) => r.speech_share, fmt: (r) => pct(r.speech_share) },
  {
    id: 'moments_per_min',
    label: 'evidence/min',
    get: (r) => r.moments_per_min,
    fmt: (r) => n(r.moments_per_min, 1),
  },
];

/**
 * The reels behind the distributions, sortable.
 *
 * Sorting is the point: a median is a claim about a set, and the fastest way to
 * decide whether to believe it is to sort by that column and look at both ends.
 * Nulls sort last in *both* directions — "no shots were detected" is not the
 * slowest cut rate, and letting it sit at the top of an ascending sort would
 * invite exactly that reading.
 */
function ReelTable({
  rows,
  total,
  paramsNow,
}: {
  rows: ReelFeatures[];
  total: number;
  paramsNow: (patch?: Record<string, unknown>) => Record<string, unknown>;
}) {
  const [sort, setSort] = useState<{ id: string; dir: 1 | -1 }>({ id: 'cuts_per_min', dir: -1 });
  const sorted = useMemo(() => {
    const col = RCOLS.find((c) => c.id === sort.id);
    const out = [...rows];
    if (!col) return out;
    out.sort((a, b) => {
      const x = col.get(a);
      const y = col.get(b);
      if (x === null && y === null) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (x - y) * sort.dir;
    });
    return out;
  }, [rows, sort]);

  if (!rows.length) return null;
  return (
    <section>
      <div className="section-h">
        <h2>Reel by reel</h2>
        <span className="count">
          {fmtCount(rows.length)}
          {total > rows.length ? ` ${ofN(total)}` : ''}
        </span>
      </div>
      <div className="stu-rt">
        <div className="stu-rt-h">
          <span>reel</span>
          {RCOLS.map((c) => (
            <button
              key={c.id}
              className={sort.id === c.id ? 'on' : undefined}
              onClick={() => setSort((s) => ({ id: c.id, dir: s.id === c.id ? ((s.dir * -1) as 1 | -1) : -1 }))}
              title={`sort by ${c.label}`}
            >
              {c.label}
              {sort.id === c.id ? (sort.dir === -1 ? ' ↓' : ' ↑') : ''}
            </button>
          ))}
          <span>channels</span>
        </div>
        {sorted.map((r) => (
          <a
            key={r.video_key}
            className="stu-rt-r"
            href={href('studio', { params: paramsNow({ mode: 'reel', key: r.video_key }) })}
          >
            <span className="stu-rt-t" title={`${r.title}${r.creator ? ` — ${r.creator}` : ''}`}>
              {r.title || r.video_key}
            </span>
            {RCOLS.map((c) => (
              <span key={c.id} className={`stu-rt-v${c.get(r) === null ? ' cell-null' : ''}`}>
                {c.fmt(r)}
              </span>
            ))}
            <span className="stu-rt-ch">
              {r.channels.map((s) => (
                <i
                  key={s}
                  className="stu-dot"
                  style={{ background: channelVar(s) }}
                  title={`${s} — ${pct(r.shares[s])} of runtime`}
                />
              ))}
            </span>
          </a>
        ))}
      </div>
    </section>
  );
}
// ─────────────────────────────────────────────────────────────────────────────
// Reel — one reel taken apart
// ─────────────────────────────────────────────────────────────────────────────

function ReelPane({
  state,
  hasKey,
  rows,
  paramsNow,
}: {
  state: FetchState<DeconstructResponse>;
  hasKey: boolean;
  rows: ReelFeatures[];
  paramsNow: (patch?: Record<string, unknown>) => Record<string, unknown>;
}) {
  if (!hasKey) {
    return (
      <div className="state-box">
        <Scissors size={22} className="dim" />
        <strong>Pick a reel to take apart.</strong>
        <span>Cut rhythm, which channel holds each stretch, and where it changes what it is doing.</span>
        {rows.length > 0 && (
          <div className="stu-picks">
            {rows.slice(0, 6).map((r) => (
              <a
                key={r.video_key}
                className="btn btn-sm"
                href={href('studio', { params: paramsNow({ mode: 'reel', key: r.video_key }) })}
              >
                {clip(r.title || r.video_key, 34)}
              </a>
            ))}
          </div>
        )}
      </div>
    );
  }
  const d = state.data;
  return (
    <PaneState state={state} empty="That reel is not in the index.">
      {d && (
        <>
          <Notes notes={d.notes} />
          <ReelHead d={d} />
          <PacingPanel d={d} />
          <Heatmap d={d} />
          <SectionList d={d} />
          <HookPanel d={d} />
          <GapsAndClaims d={d} />
        </>
      )}
    </PaneState>
  );
}

function ReelHead({ d }: { d: DeconstructResponse }) {
  const v = d.video;
  return (
    <>
      <div className="stu-head">
        <div className="stu-head-l">
          <h1 className="view-title">{v.title || v.video_key}</h1>
          <div className="view-sub">
            {[v.creator, v.category].filter(Boolean).join(' · ')}
            {v.creator || v.category ? ' · ' : ''}
            {fmtDur(d.duration)}
            {/* Where the runtime came from matters: a duration derived from the
                last timestamp in the evidence is a lower bound, and every rate
                on this screen divides by it. */}
            <span className="dim"> · runtime from {d.duration_from}</span>
          </div>
          {v.caption && <div className="stu-caption">{clip(v.caption, 320)}</div>}
          <div className="stu-chips">
            {Object.entries(v.sources)
              .sort((a, b) => b[1] - a[1])
              .map(([s, count]) => (
                <Chan key={s} source={s} n={count} />
              ))}
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => watch(v.video_key, 0)}>
          <Film size={13} /> watch
        </button>
      </div>

      <div className="stat-row">
        <div className="stat">
          <span className="n">{fmtCount(d.moments)}</span>
          <span className="l">moments</span>
        </div>
        <div className="stat">
          <span className="n">{n(d.density.moments_per_min, 1)}</span>
          <span className="l">evidence / min</span>
        </div>
        <div className="stat">
          <span className="n">{n(d.density.words_per_s, 2)}</span>
          <span className="l">words / s</span>
        </div>
        <div className="stat">
          <span className="n">{fmtCount(d.density.channels_used)}</span>
          <span className="l">channels used</span>
        </div>
      </div>
    </>
  );
}
function PacingPanel({ d }: { d: DeconstructResponse }) {
  const p = d.pacing;
  const hold = p.longest_hold;
  return (
    <section>
      <div className="section-h">
        <h2>Cutting</h2>
        <span className="count">
          {p.shots ? `${plural(p.shots, 'shot')} · ${plural(p.cuts, 'cut')}` : 'no shots detected'}
        </span>
      </div>
      {!p.shots ? (
        <div className="view-hint">
          No shot boundaries were detected for this reel, so cut rate, shot length and rhythm are absent
          rather than zero. Running the <strong>shots</strong> pass on it fills them in.
        </div>
      ) : (
        <div className="stu-pace">
          <dl className="kv stu-kv">
            <dt>cuts / min</dt>
            <dd>{n(p.cuts_per_min, 1)}</dd>
            <dt>shot length</dt>
            <dd>
              {n(p.shot_len.median, 2, 's')} <em className="dim">median of {fmtCount(p.shot_len.n)}</em>
            </dd>
            <dt>rhythm</dt>
            <dd>
              {n(p.regularity, 2)} <em className="dim">1 − CV of shot length</em>
            </dd>
            <dt>covered</dt>
            <dd title="Shot seconds over runtime. Can exceed 100% — a detector's last boundary may run past the recorded duration.">
              {fmtDur(p.covered_s)} <em className="dim">{pct(p.coverage)} of runtime</em>
            </dd>
          </dl>
          <div className="stu-pace-r">
            <div className="dim">shot length across this reel</div>
            <DistBar s={p.shot_len} fmt={(v) => n(v, 2, 's')} />
            <div className="stu-pace-x">
              <span>{n(p.shot_len.min, 2, 's')}</span>
              <span>{n(p.shot_len.max, 2, 's')}</span>
            </div>
            {hold && (
              <button
                className="btn-ghost stu-jump"
                onClick={() => watch(d.video.video_key, hold.t0)}
                title="open the player on the longest uncut stretch"
              >
                <Timer size={12} /> longest hold {n(hold.len, 1, 's')} at {fmtT(hold.t0)}
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
/**
 * Channel × time. The one picture that answers "how is this reel built".
 *
 * A row per observer, a column per time bin, opacity as occupancy — so a reel
 * that talks over stock footage and one that cuts silent text cards look nothing
 * alike at a glance, which is the whole reason to draw it instead of tabulating
 * it. Every cell is clickable and opens the player at that bin's start, because
 * the question that follows "the visual row goes dark here" is always "what
 * happens there".
 */
function Heatmap({ d }: { d: DeconstructResponse }) {
  const { channels, bins, bin_s, matrix } = d.timeline;
  if (!channels.length || !matrix.length) {
    return (
      <section>
        <div className="section-h">
          <h2>Channels over time</h2>
        </div>
        <div className="view-hint">
          Nothing in this reel carries a timestamp, so there is no timeline to draw. Untimed evidence — a
          caption, a category — still appears in the channel list above.
        </div>
      </section>
    );
  }
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return (
    <section>
      <div className="section-h">
        <h2>Channels over time</h2>
        <span className="count">
          {fmtCount(bins)} bins of {n(bin_s, 2)}s · click to open the player there
        </span>
      </div>
      <div className="stu-hm">
        {channels.map((c, ci) => (
          <Fragment key={c}>
            <span className="stu-hm-l">
              <Chan source={c} />
            </span>
            <div className="stu-hm-row">
              {matrix.map((row, bi) => {
                const v = Math.max(0, Math.min(1, row[ci] ?? 0));
                const t0 = bi * bin_s;
                return (
                  <button
                    key={bi}
                    className="stu-hm-c"
                    style={{ background: channelVar(c), opacity: v === 0 ? 0.05 : 0.18 + 0.82 * v }}
                    onClick={() => watch(d.video.video_key, t0)}
                    title={`${c} · ${fmtT(t0)}–${fmtT(t0 + bin_s)} · ${pct(v)} of the bin`}
                    aria-label={`${c} at ${fmtT(t0)}, ${pct(v)} occupied`}
                  />
                );
              })}
            </div>
          </Fragment>
        ))}
        <span />
        <div className="stu-hm-axis">
          {ticks.map((f) => (
            <span key={f} style={{ left: `${f * 100}%` }}>
              {fmtT(f * d.duration)}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}
/**
 * The change points, found rather than assumed.
 *
 * Binary segmentation over the same occupancy matrix the heatmap draws: the
 * split that most reduces within-segment variance, recursively, while each split
 * still explains enough of what remains to be worth naming. The labels describe
 * what occupies a stretch — `speech + caption-led` — and deliberately stop
 * short of saying what it *is*, because "the reveal" is a reading and this
 * screen only reports measurements.
 */
function SectionList({ d }: { d: DeconstructResponse }) {
  const secs = d.sections;
  if (!secs.length) {
    return (
      <section>
        <div className="section-h">
          <h2>Where it changes</h2>
        </div>
        <div className="view-hint">
          Too little timed evidence to find a change point in this reel — segmentation needs a timeline it
          can measure variance along.
        </div>
      </section>
    );
  }
  return (
    <section>
      <div className="section-h">
        <h2>Where it changes</h2>
        <span className="count">
          {secs.length} section{secs.length === 1 ? '' : 's'}
        </span>
      </div>
      <div className="stu-strip">
        {secs.map((s) => (
          <button
            key={s.n}
            className="stu-strip-s"
            style={{ flexGrow: Math.max(s.share, 0.02), background: channelVar(s.lead) }}
            onClick={() => watch(d.video.video_key, s.t0)}
            title={`${s.n}. ${s.label} · ${fmtT(s.t0)}–${fmtT(s.t1)} · ${n(s.len, 1, 's')}`}
          >
            {s.n}
          </button>
        ))}
      </div>
      <div className="row-list">
        {secs.map((s) => (
          <div key={s.n} className="row-item stu-sec">
            <span className="stu-sec-n" style={{ background: channelVar(s.lead) }}>
              {s.n}
            </span>
            <button className="btn-ghost stu-sec-t" onClick={() => watch(d.video.video_key, s.t0)}>
              {fmtT(s.t0)} – {fmtT(s.t1)}
            </button>
            <span className="stu-sec-l">{s.label}</span>
            <span className="spacer" />
            <span className="stu-sec-mix">
              {Object.entries(s.mix)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 4)
                .map(([k, v]) => (
                  <span key={k} className="stu-mix" title={`${k} occupies ${pct(v)} of this section`}>
                    <i className="stu-dot" style={{ background: channelVar(k) }} />
                    {pct(v)}
                  </span>
                ))}
            </span>
            <span className="stu-sec-len">{n(s.len, 1, 's')}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
function HookPanel({ d }: { d: DeconstructResponse }) {
  const h = d.hook;
  return (
    <section>
      <div className="section-h">
        <h2>The first {n(h.window_s, 1)}s</h2>
        <span className="count">
          {fmtCount(h.moments)} moment{h.moments === 1 ? '' : 's'} land there
        </span>
      </div>
      <div className="stu-two">
        <dl className="kv stu-kv">
          <dt>on the first frame</dt>
          <dd>
            {h.first_frame_channels.length ? (
              <span className="stu-chips">
                {h.first_frame_channels.map((s) => (
                  <Chan key={s} source={s} />
                ))}
              </span>
            ) : (
              <span className="cell-null">nothing is timed at t=0</span>
            )}
          </dd>
          <dt>in the window</dt>
          <dd>
            {h.channels.length ? (
              <span className="stu-chips">
                {h.channels.map((s) => (
                  <Chan key={s} source={s} />
                ))}
              </span>
            ) : (
              <span className="cell-null">no evidence in the window</span>
            )}
          </dd>
          <dt>cuts</dt>
          <dd>{fmtCount(h.cuts)}</dd>
          <dt>words</dt>
          <dd>
            {fmtCount(h.words)} <em className="dim">{n(h.words_per_s, 2)} / s</em>
          </dd>
          <dt>first word at</dt>
          <dd>
            {h.first_speech_at === null ? (
              <span className="cell-null">no speech in the window</span>
            ) : (
              fmtT(h.first_speech_at)
            )}
          </dd>
          <dt>opens silent</dt>
          <dd>
            {h.silent_open === null ? (
              // Nothing on this reel is placed on the clock, so there is no t=0
              // to look at. `no` here would be the answer to a question the
              // pipeline never got far enough to ask.
              <span className="cell-null">nothing is timed yet</span>
            ) : h.silent_open ? (
              'yes'
            ) : (
              'no'
            )}
          </dd>
        </dl>
        <div className="stu-quotes">
          {h.text.length ? (
            h.text.map((t, i) => (
              <button
                key={i}
                className="stu-quote"
                onClick={() => watch(d.video.video_key, t.t)}
                title="open the player here"
              >
                <span className="stu-quote-h">
                  <Chan source={t.source} />
                  <span className="stu-quote-t">{fmtT(t.t)}</span>
                </span>
                <span className="stu-quote-x">{clip(t.text, 220)}</span>
              </button>
            ))
          ) : (
            <div className="dim">No words, captions or on-screen text in the opening window.</div>
          )}
        </div>
      </div>
    </section>
  );
}

function GapsAndClaims({ d }: { d: DeconstructResponse }) {
  const { gaps, claims } = d;
  return (
    <section>
      <div className="section-h">
        <h2>Blind spots and entities</h2>
      </div>
      <div className="stu-two">
        <div className="panel">
          <div className="panel-h">
            <Activity size={13} /> unobserved stretches
            <span className="spacer" />
            <span className="dim">{fmtCount(gaps.length)}</span>
          </div>
          {gaps.length ? (
            <div className="stu-gaps">
              {gaps.map((g, i) => (
                <button key={i} className="stu-gap" onClick={() => watch(d.video.video_key, g.t0)}>
                  {fmtT(g.t0)} – {fmtT(g.t1)} <em>{n(g.len, 1, 's')}</em>
                </button>
              ))}
            </div>
          ) : (
            <div className="dim">Every second of this reel has some evidence on it.</div>
          )}
        </div>
        <div className="panel">
          <div className="panel-h">
            <Sparkles size={13} /> entities
            <span className="spacer" />
            <span className="dim">{fmtCount(claims.length)}</span>
          </div>
          {claims.length ? (
            <div className="stu-claims">
              {claims.map((c, i) => (
                <button
                  key={`${c.kind}-${c.name}-${i}`}
                  className="stu-claim"
                  // An untimed claim opens the reel with no `t` at all rather
                  // than seeking to zero. The two look the same on screen for a
                  // moment and mean different things: `t=0.00` is a claim that
                  // this entity appears at the very start, which for a claim the
                  // server could not place anywhere is a claim it did not make.
                  onClick={() => watch(d.video.video_key, c.t0 ?? undefined)}
                  title={
                    c.t0 === null
                      ? `${c.kind} · confidence ${n(c.confidence, 2)} · whole reel`
                      : `${c.kind} · confidence ${n(c.confidence, 2)} · ${fmtT(c.t0)}–${fmtT(c.t1)}`
                  }
                >
                  <span className="stu-claim-n">{c.name}</span>
                  <span className="stu-claim-k">{c.kind}</span>
                  <span className="stu-claim-c">{n(c.confidence, 2)}</span>
                </button>
              ))}
            </div>
          ) : (
            <div className="dim">The knowledge graph has not named anything in this reel yet.</div>
          )}
        </div>
      </div>
    </section>
  );
}
// ─────────────────────────────────────────────────────────────────────────────
// Script — the scope as a beat sheet
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Copy, with an honest failure state.
 *
 * `navigator.clipboard` needs a secure context, and a desktop window served from
 * `http://127.0.0.1` counts as one — but a browser that refuses still has to say
 * so, because a copy button that silently does nothing is worse than no button.
 */
function CopyButton({ text }: { text: string }) {
  const [msg, setMsg] = useState('');
  useEffect(() => {
    if (!msg) return;
    const id = window.setTimeout(() => setMsg(''), 1800);
    return () => window.clearTimeout(id);
  }, [msg]);
  return (
    <button
      className="btn btn-primary"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setMsg('copied');
        } catch {
          setMsg('copy blocked — select the sheet below');
        }
      }}
    >
      <Copy size={13} /> {msg || 'copy the sheet'}
    </button>
  );
}

function ScriptPane({ state }: { state: FetchState<ScriptResponse> }) {
  const d = state.data;
  return (
    <PaneState state={state} empty="Nothing in scope to measure a beat sheet from.">
      {d &&
        (d.reels === 0 ? (
          <div className="state-box">
            <strong>No reels in this scope.</strong>
            <span>{d.scope.note}</span>
          </div>
        ) : (
          <>
            <Notes notes={d.notes} />
            <div className="stu-head">
              <div className="stu-head-l">
                {/* `outline.head` is the provenance line the server puts at the
                    top of the pasteable text, not a title — so it stays out of
                    the h1 and the sub says the same thing with the spread. */}
                <h1 className="view-title">Beat sheet</h1>
                <div className="view-sub">
                  target {n(d.target_s, 1, 's')} — the median of {fmtCount(d.duration.n)} reel
                  {d.duration.n === 1 ? '' : 's'}
                  {d.duration.n >= 2
                    ? ` · p10 ${n(d.duration.p10, 1, 's')} – p90 ${n(d.duration.p90, 1, 's')}`
                    : ''}
                </div>
              </div>
              <CopyButton text={d.outline.text} />
            </div>

            <div className="section-h">
              <h2>
                <ListOrdered size={14} className="dim" /> Beats
              </h2>
              <span className="count">{d.method.slots}</span>
            </div>
            <div className="stu-beats">
              {d.beats.map((b) => (
                <BeatCard key={b.name} b={b} />
              ))}
            </div>

            <OutlineBlock outline={d.outline} method={d.method} />
          </>
        ))}
    </PaneState>
  );
}
function BeatCard({ b }: { b: Beat }) {
  return (
    <div className="stu-beat">
      <div className="stu-beat-h">
        <span className="stu-beat-n">{b.name}</span>
        <span className="stu-beat-t">
          {fmtT(b.t0)} – {fmtT(b.t1)} · {n(b.len, 1, 's')}
        </span>
        <span className="spacer" />
        <span className="stu-beat-p">
          {pct(b.p0)}–{pct(b.p1)} of runtime
        </span>
      </div>

      {b.lead ? (
        <div className="stu-beat-lead">
          <Chan source={b.lead} /> leads here in {pct(b.lead_rate)} of reels{' '}
          <em className="dim">{ofN(b.voters)} with evidence in this slot</em>
        </div>
      ) : (
        <div className="dim">
          No channel leads this slot often enough to name
          {b.voters ? ` — ${fmtCount(b.voters)} reel${b.voters === 1 ? '' : 's'} had evidence here` : ''}.
        </div>
      )}

      {b.leads.length > 1 && (
        <div className="stu-beat-leads">
          {b.leads.map((l) => (
            <RateRow key={l.source} source={l.source} count={l.n} rate={l.rate} of={b.voters} />
          ))}
        </div>
      )}

      <dl className="kv stu-kv">
        <dt>cuts</dt>
        <dd>
          {n(b.cuts.median, 0)} <em className="dim">median {ofN(b.cuts.n)}</em>
        </dd>
        <dt>words</dt>
        <dd>
          {n(b.words.median, 0)} <em className="dim">median {ofN(b.words.n)}</em>
        </dd>
      </dl>

      {b.phrases.length > 0 && (
        <>
          <div className="stu-beat-sub">what tends to be said here</div>
          <PhraseList phrases={b.phrases} />
        </>
      )}

      {b.examples.length > 0 && (
        <>
          <div className="stu-beat-sub">
            <Quote size={12} /> real moments from this slot
          </div>
          <div className="stu-quotes">
            {b.examples.map((ex, i) => (
              <button
                key={`${ex.video_key}-${i}`}
                className="stu-quote"
                onClick={() => watch(ex.video_key, ex.t)}
                title={`${ex.title} at ${fmtT(ex.t)} — open the player there`}
              >
                <span className="stu-quote-h">
                  <Chan source={ex.source} />
                  <span className="stu-quote-t">{fmtT(ex.t)}</span>
                  <span className="stu-quote-src">{clip(ex.title || ex.video_key, 44)}</span>
                </span>
                <span className="stu-quote-x">{clip(ex.text, 240)}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
/**
 * The sheet itself, twice: as rows, and as the plain text the copy button hands
 * over. Both come from `outline`, which the server renders from `beats` — so the
 * thing you paste into a notes app is the same thing you just read, rather than
 * a second summary that could drift from it.
 */
function OutlineBlock({
  outline,
  method,
}: {
  outline: ScriptResponse['outline'];
  method: ScriptResponse['method'];
}) {
  return (
    <section>
      <div className="section-h">
        <h2>The sheet</h2>
        <span className="count">{method.numbers}</span>
      </div>
      <div className="stu-caption">{outline.head}</div>
      <div className="stu-outline">
        {outline.lines.map((l) => (
          <div key={l.name} className="stu-ol">
            <div className="stu-ol-h">
              <span className="stu-ol-n">{l.name}</span>
              <span className="stu-ol-x">{l.headline}</span>
            </div>
            {l.points.length > 0 && (
              <ul className="stu-ol-p">
                {l.points.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
      <details className="stu-raw">
        <summary>
          <SlidersHorizontal size={12} /> as plain text
        </summary>
        <pre className="font-mono">{outline.text}</pre>
      </details>
      <div className="view-hint">
        {method.prose}. {method.phrases}.
      </div>
    </section>
  );
}

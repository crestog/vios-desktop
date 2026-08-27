/**
 * views/Roadmap.tsx — the archive read as a curriculum.
 *
 * The Graph tab answers "what is connected to what". This screen asks the same
 * two derived tables a different question: *in what order should somebody watch
 * this to end up knowing something*. `atlas/roadmap.py` infers that order from
 * subsumption — if nearly every reel that mentions "compound interest" also
 * mentions "interest", but not the reverse, then interest comes first — and the
 * result is a layered DAG of concepts, each carrying the exact passages worth
 * watching for it.
 *
 * Three decisions worth reading before editing:
 *
 *   - **The order is a claim, so its evidence is shown.** A prerequisite is not
 *     presented as a fact; it is presented as two conditional probabilities and
 *     the gap between them, because "89% of reels about this also cover that,
 *     but only 31% the other way round" is checkable and a lone 0.27 is not.
 *
 *   - **`said` is never hidden.** A passage is either the moment the concept is
 *     literally spoken (FTS found it) or merely a strong passage of a reel that
 *     carries the concept (it was mined from a list column and may be silent).
 *     The interface says which, rather than implying a quote that does not
 *     exist — the same honesty the rest of the app is built on.
 *
 *   - **Progress is instant but authoritative.** Ticking a step off paints
 *     immediately from a local overlay, fires the POST, then reloads the plan —
 *     which is a server-side cache hit that re-joins the ticks onto the derived
 *     structure. The overlay is cleared the moment real data returns, so the
 *     checkbox never disagrees with the database for longer than one request.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  Check,
  ChevronRight,
  Circle,
  Clock,
  GraduationCap,
  Layers,
  Play,
  RotateCcw,
  SkipForward,
  Target,
  X,
} from 'lucide-react';
import type {
  RoadmapPrereq,
  RoadmapStep,
  RoadmapStepDetail,
  VideoItem,
} from '../types';
import type { ViewProps } from '../lib/router';
import { go, href, num, watch } from '../lib/router';
import {
  clearRoadmapProgress,
  getRoadmap,
  getRoadmapGoals,
  getRoadmapStep,
  markRoadmapStep,
} from '../lib/api';
import { useDebounced, useFetch } from '../lib/useFetch';
import { channelOf, chipClass } from '../lib/channels';
import { clip, fmtCount, fmtDur, fmtPct, fmtT, plural } from '../lib/format';

// The server's own defaults. Kept here so the sliders read the same numbers the
// plan was built with, and so a value equal to the default drops out of the URL
// rather than pinning a link to a choice the user never made.
const BREADTH = 60;
const MIN_SUPPORT = 2;

/** `done` and `skip` both count as "handled"; the empty string is untouched. */
type Mark = 'done' | 'skip' | '';

export default function RoadmapView({ route }: ViewProps) {
  const p = route.params;
  const goal = p.get('goal') || '';
  const breadth = num(p, 'breadth') ?? BREADTH;
  const minSup = num(p, 'min') ?? MIN_SUPPORT;
  const stepId = p.get('step') || '';

  // The goal box types into the URL, debounced, the same way Search and Data do
  // — so a plan is a link, Back leaves the roadmap rather than walking through
  // keystrokes, and a pasted `?goal=` reproduces exactly this plan.
  const [goalInput, setGoalInput] = useState(goal);
  useEffect(() => setGoalInput(goal), [goal]);
  const typedGoal = useDebounced(goalInput, 250);

  const paramsNow = (patch: Record<string, unknown> = {}): Record<string, unknown> => {
    const all: Record<string, unknown> = { goal, breadth, min: minSup, step: stepId, ...patch };
    if (Number(all.breadth) === BREADTH) all.breadth = '';
    if (Number(all.min) === MIN_SUPPORT) all.min = '';
    return all;
  };
  const setParam = (patch: Record<string, unknown>, replace = false) =>
    go('roadmap', { params: paramsNow(patch), replace });

  useEffect(() => {
    if (typedGoal === goal) return;
    // A new goal invalidates the selected step: a step id is a graph node id,
    // which survives, but the scope it was read in has changed, so reopen it
    // fresh rather than showing last scope's numbers under a new heading.
    setParam({ goal: typedGoal, step: '' }, true);
  }, [typedGoal]); // eslint-disable-line react-hooks/exhaustive-deps

  const plan = useFetch((s) => getRoadmap(goal, breadth, minSup, s), [goal, breadth, minSup]);
  const goals = useFetch((s) => getRoadmapGoals(16, s), [], { enabled: !goal });

  // ── Progress overlay ──────────────────────────────────────────────────────
  // The authoritative marks come back on the plan; this is only the in-flight
  // difference, cleared as soon as a reload lands the real state. Keyed by step
  // id, not by index, because the plan re-sorts on rebuild.
  const [overlay, setOverlay] = useState<Record<string, Mark>>({});
  const [clearing, setClearing] = useState(false);
  useEffect(() => setOverlay({}), [plan.data]);

  const steps = plan.data?.steps ?? [];
  const byId = useMemo(() => new Map(steps.map((s) => [s.id, s])), [steps]);
  const stateOf = (id: string): Mark => {
    if (id in overlay) return overlay[id];
    return (byId.get(id)?.state as Mark) || '';
  };

  async function mark(id: string, next: Mark) {
    setOverlay((o) => ({ ...o, [id]: next }));
    try {
      await markRoadmapStep(id, next, goal);
      plan.reload();
    } catch {
      // Put it back the way it was — a failed write must not leave a lie on
      // screen. The next reload will confirm the real state regardless.
      setOverlay((o) => {
        const copy = { ...o };
        delete copy[id];
        return copy;
      });
    }
  }

  async function clearAll() {
    if (clearing) return;
    setClearing(true);
    try {
      await clearRoadmapProgress();
      plan.reload();
    } finally {
      setClearing(false);
    }
  }

  // Counts recomputed locally so the progress rail and the bar respond to a tick
  // in the same frame it was clicked, rather than after the reload round-trip.
  const total = steps.length;
  const done = steps.filter((s) => stateOf(s.id) === 'done').length;
  const skipped = steps.filter((s) => stateOf(s.id) === 'skip').length;
  const marked = done + skipped;
  const remainingSec = steps.filter((s) => !stateOf(s.id)).reduce((a, s) => a + (s.seconds || 0), 0);

  // "Ready" is a step with nothing left blocking it. Recomputed against the
  // overlay so ticking a prerequisite lights up what it unlocks immediately; a
  // prerequisite dropped from the plan by the breadth cap is treated as blocking
  // (we cannot see its mark from here), which errs toward not over-promising.
  const readySet = useMemo(() => {
    const out = new Set<string>();
    for (const s of steps) {
      if (stateOf(s.id)) continue;
      if (s.prereq.every((pr) => (byId.has(pr.id) ? !!stateOf(pr.id) : false))) out.add(s.id);
    }
    return out;
  }, [steps, overlay, byId]); // eslint-disable-line react-hooks/exhaustive-deps

  const data = plan.data;
  const isGoal = data?.mode === 'goal';

  return (
    <div className="view view-split">
      <div className="view-bar">
        <GraduationCap size={14} className="dim" />
        <strong>{isGoal ? `Learning: ${data?.goal}` : 'The whole archive, in order'}</strong>
        {data && total > 0 && (
          <span className="dim">
            {fmtCount(total)} concepts · {data.stats.stages} stages ·{' '}
            {fmtDur(remainingSec) || '0s'} left
          </span>
        )}

        <span className="spacer" />

        <div className="search-box-wrap">
          <Target size={13} className="sbw-icon" />
          <input
            className="input-text search-box"
            value={goalInput}
            onChange={(e) => setGoalInput(e.target.value)}
            placeholder="a goal to plan for — “learn to edit hooks”"
            spellCheck={false}
            aria-label="Plan for a goal"
          />
          {goalInput && (
            <button className="btn-icon sbw-clear" onClick={() => setGoalInput('')} title="whole archive">
              <X size={12} />
            </button>
          )}
        </div>

        <label className="dens" title="how many concepts to order — the rest stay one click away in the Graph tab">
          <Layers size={12} /> breadth
          <input
            type="range"
            min={12}
            max={200}
            step={4}
            value={breadth}
            onChange={(e) => setParam({ breadth: Number(e.target.value), step: '' }, true)}
          />
          <span className="dens-n">{breadth}</span>
        </label>

        <label className="dens" title="how many reels a concept must appear in before it earns a step">
          support ≥
          <input
            type="range"
            min={1}
            max={12}
            step={1}
            value={minSup}
            onChange={(e) => setParam({ min: Number(e.target.value), step: '' }, true)}
          />
          <span className="dens-n">{minSup}</span>
        </label>
      </div>

      <div className="split">
        <aside className="rail rail-left" aria-label="Progress and goals">
          {/* Progress — computed locally, so a tick moves the bar at once. */}
          {total > 0 && (
            <div className="rail-block">
              <div className="rail-h">
                progress
                <span className="rail-n">{fmtPct(marked, total)}</span>
              </div>
              <div className="rm-bar" title={`${marked} of ${total} handled`}>
                <span className="rm-bar-done" style={{ width: `${(done / total) * 100}%` }} />
                <span className="rm-bar-skip" style={{ width: `${(skipped / total) * 100}%` }} />
              </div>
              <dl className="kv rm-kv">
                <dt>watched</dt>
                <dd>{fmtCount(done)}</dd>
                <dt>skipped</dt>
                <dd>{fmtCount(skipped)}</dd>
                <dt>left</dt>
                <dd>{fmtCount(total - marked)}</dd>
                <dt>to watch</dt>
                <dd>{fmtDur(remainingSec) || '0s'}</dd>
              </dl>
              {marked > 0 && (
                <button className="btn-ghost rm-clear" onClick={clearAll} disabled={clearing}>
                  <RotateCcw size={12} /> {clearing ? 'clearing…' : 'clear my progress'}
                </button>
              )}
            </div>
          )}

          {/* What to watch next: ready steps, the only question a curriculum
              has to answer at any single moment. */}
          {readySet.size > 0 && (
            <div className="rail-block">
              <div className="rail-h">
                ready to watch
                <span className="rail-n">{readySet.size}</span>
              </div>
              <div className="rm-ready">
                {steps
                  .filter((s) => readySet.has(s.id))
                  .slice(0, 8)
                  .map((s) => (
                    <a
                      key={s.id}
                      className={`rm-ready-row${s.id === stepId ? ' is-active' : ''}`}
                      href={href('roadmap', { params: paramsNow({ step: s.id }) })}
                    >
                      <span className="rm-ready-l">{s.label}</span>
                      <span className="rm-ready-w">{fmtCount(s.videos)}</span>
                    </a>
                  ))}
              </div>
            </div>
          )}

          {/* Goals worth offering, before anything is typed. */}
          {!goal && (
            <div className="rail-block">
              <div className="rail-h">or aim at one thing</div>
              <div className="rail-note">
                Plan over just the reels about a goal. Support, order and stages are all recomputed
                inside that corner of the archive.
              </div>
              {goals.loading && goals.first ? (
                <div className="rail-note">looking…</div>
              ) : (
                <div className="rm-goals">
                  {(goals.data?.goals ?? []).map((g) => (
                    <a
                      key={g.label}
                      className="rm-goal"
                      href={href('roadmap', { params: { goal: g.label } })}
                      title={`${plural(g.videos, 'reel')} ${g.videos === 1 ? 'touches' : 'touch'} ${g.label}`}
                    >
                      {g.label}
                      <span className="rm-goal-n">{fmtCount(g.videos)}</span>
                    </a>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* How the order is inferred — stated once, plainly. */}
          {total > 0 && (
            <div className="rail-block">
              <div className="rail-h">how the order is found</div>
              <div className="rail-note">
                No syllabus is written and no model is asked. If almost every reel about B also
                covers A, but plenty about A never touch B, then A is the broader idea and comes
                first. Each “after” below shows the two probabilities that decided it.
              </div>
            </div>
          )}
        </aside>

        <div className="split-main">
          {plan.error ? (
            <div className="state-box err">
              <div className="head">Could not build a plan</div>
              <div>{plan.error}</div>
            </div>
          ) : plan.first && plan.loading ? (
            <div className="rm-stages">
              {Array.from({ length: 3 }, (_, i) => (
                <div className="skel" style={{ height: 180, margin: '10px 0' }} key={i} />
              ))}
            </div>
          ) : !data || total === 0 ? (
            <div className="state-box">
              <div className="head">Nothing to order yet</div>
              <div>
                {data?.note ||
                  'The roadmap reads the same graph the Graph tab does. Build the index and the graph first, or widen the goal.'}
              </div>
            </div>
          ) : (
            <>
              {(data.note || data.scope_note) && (
                <div className="view-hint">
                  {data.note || data.scope_note}
                  {isGoal && data.scope_note && data.note ? ` · ${data.scope_note}` : ''}
                </div>
              )}

              <div className="rm-stages">
                {data.stages.map((st) => {
                  const stSteps = st.steps.map((id) => byId.get(id)).filter(Boolean) as RoadmapStep[];
                  const stDone = stSteps.filter((s) => stateOf(s.id) === 'done').length;
                  return (
                    <section className="rm-stage" id={`rm-stage-${st.level}`} key={st.level}>
                      <header className="rm-stage-h">
                        <span className="rm-stage-lv">{st.level}</span>
                        <div className="rm-stage-t">
                          <strong>{st.title}</strong>
                          <span className="dim">{st.why}</span>
                        </div>
                        <span className="rm-stage-meta">
                          {stDone}/{stSteps.length} · {fmtDur(st.seconds) || '0s'}
                        </span>
                      </header>
                      <div className="rm-steps">
                        {stSteps.map((s) => (
                          <StepRow
                            key={s.id}
                            step={s}
                            state={stateOf(s.id)}
                            ready={readySet.has(s.id)}
                            selected={s.id === stepId}
                            selectHref={href('roadmap', { params: paramsNow({ step: s.id }) })}
                            onToggleDone={() => mark(s.id, stateOf(s.id) === 'done' ? '' : 'done')}
                            onToggleSkip={() => mark(s.id, stateOf(s.id) === 'skip' ? '' : 'skip')}
                          />
                        ))}
                      </div>
                    </section>
                  );
                })}
              </div>

              <div className="rm-foot dim">
                Built in {Math.round(data.built_ms)} ms{data.cached ? ' · from cache' : ''} · reads{' '}
                {plural(data.stats.scope_videos, 'reel')}
              </div>
            </>
          )}
        </div>

        {stepId && (
          <StepInspector
            stepId={stepId}
            planStep={byId.get(stepId)}
            goal={goal}
            state={stateOf(stepId)}
            onMark={mark}
            onSelect={(id) => setParam({ step: id })}
            onClose={() => setParam({ step: '' })}
          />
        )}
      </div>
    </div>
  );
}

// ── One step, in the stage list ─────────────────────────────────────────────
function StepRow({
  step,
  state,
  ready,
  selected,
  selectHref,
  onToggleDone,
  onToggleSkip,
}: {
  step: RoadmapStep;
  state: Mark;
  ready: boolean;
  selected: boolean;
  selectHref: string;
  onToggleDone: () => void;
  onToggleSkip: () => void;
}) {
  const done = state === 'done';
  const skip = state === 'skip';
  return (
    <div
      className={`rm-step${done ? ' is-done' : ''}${skip ? ' is-skip' : ''}${
        selected ? ' is-selected' : ''
      }${ready ? ' is-ready' : ''}`}
    >
      <button
        className="rm-check"
        onClick={onToggleDone}
        title={done ? 'mark not watched' : 'mark watched'}
        aria-pressed={done}
      >
        {done ? <Check size={13} /> : <Circle size={13} />}
      </button>

      <a className="rm-step-main" href={selectHref}>
        <span className="rm-step-label">{step.label}</span>
        <span className="rm-step-meta">
          <span className="rm-kind">{step.kind === 'hashtag' ? '#tag' : step.group || 'concept'}</span>
          <span title="reels this concept appears in">{plural(step.videos, 'reel')}</span>
          {step.share > 0 && (
            <span title="share of the reels in scope">{fmtPct(step.share, 1)}</span>
          )}
          {step.moments.length > 0 && (
            <span title={step.said ? 'a moment says this' : 'no transcript hit — strongest passages'}>
              {step.said ? 'spoken' : 'shown'}
            </span>
          )}
          {step.unlocks.length > 0 && (
            <span className="dim">unlocks {step.unlocks.length}</span>
          )}
        </span>
        {step.prereq.length > 0 && (
          <span className="rm-step-after dim">
            after {step.prereq.map((pr) => pr.label).join(', ')}
          </span>
        )}
      </a>

      {ready && !done && !skip && <span className="rm-badge">next</span>}

      <button
        className="rm-skip"
        onClick={onToggleSkip}
        title={skip ? 'un-skip' : 'skip this — I already know it'}
        aria-pressed={skip}
      >
        <SkipForward size={12} />
      </button>

      <a className="rm-open" href={selectHref} title="open this step">
        <ChevronRight size={14} />
      </a>
    </div>
  );
}

// ── The inspector: one concept in full ──────────────────────────────────────
function StepInspector({
  stepId,
  planStep,
  goal,
  state,
  onMark,
  onSelect,
  onClose,
}: {
  stepId: string;
  /** The plan's own step, which carries prereqs and unlocks the detail lacks. */
  planStep?: RoadmapStep;
  goal: string;
  state: Mark;
  onMark: (id: string, next: Mark) => void;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const detail = useFetch((s) => getRoadmapStep(stepId, goal, 0, s), [stepId, goal]);
  const d = detail.data;

  return (
    <aside className="rail rail-right rm-ins" aria-label="Step detail">
      <div className="rm-ins-head">
        <div className="rm-ins-t">
          <strong>{d?.label || stepId.split(':').pop()}</strong>
          {d && (
            <span className="dim">
              {d.kind === 'hashtag' ? 'hashtag' : d.group || 'concept'}
            </span>
          )}
        </div>
        <button className="btn-icon" onClick={onClose} title="close">
          <X size={14} />
        </button>
      </div>

      {detail.error ? (
        <div className="state-box err">
          <div className="head">Could not open this step</div>
          <div>{detail.error}</div>
        </div>
      ) : detail.first && detail.loading ? (
        <div className="rm-ins-body">
          {Array.from({ length: 5 }, (_, i) => (
            <div className="skel" style={{ height: 40, margin: '6px 0' }} key={i} />
          ))}
        </div>
      ) : !d || !d.ok ? (
        <div className="state-box">
          <div className="head">No such concept</div>
          <div>{d?.note || 'The graph does not carry this node.'}</div>
        </div>
      ) : (
        <div className="rm-ins-body">
          <div className="rm-ins-acts">
            <button
              className={`btn${state === 'done' ? ' btn-primary' : ''}`}
              onClick={() => onMark(stepId, state === 'done' ? '' : 'done')}
            >
              <Check size={13} /> {state === 'done' ? 'watched' : 'mark watched'}
            </button>
            <button
              className={`btn btn-sm${state === 'skip' ? ' btn-primary' : ''}`}
              onClick={() => onMark(stepId, state === 'skip' ? '' : 'skip')}
            >
              <SkipForward size={12} /> {state === 'skip' ? 'skipped' : 'skip'}
            </button>
          </div>

          <dl className="kv rm-ins-kv">
            <dt>reels in scope</dt>
            <dd>
              {fmtCount(d.videos_in_scope)}
              {d.videos_total !== d.videos_in_scope && (
                <span className="dim"> of {fmtCount(d.videos_total)} in all</span>
              )}
            </dd>
            <dt title="summed degree in the graph — how much of the archive this connects, not a confidence">
              connects
            </dt>
            <dd>{d.degree}</dd>
            <dt>watch time</dt>
            <dd>{fmtDur(d.seconds) || '0s'}</dd>
          </dl>

          {/* Why this comes where it does — the evidence, not a verdict. */}
          {planStep && planStep.prereq.length > 0 && (
            <div className="rm-ins-sec">
              <div className="rail-h">watch first</div>
              <div className="rm-ins-note dim">
                These come earlier because their idea contains this one. The two figures are
                P(this | that) and the reverse — the gap is why the order runs one way.
              </div>
              {planStep.prereq.map((pr) => (
                <PrereqNote key={pr.id} pr={pr} onOpen={onSelect} />
              ))}
            </div>
          )}

          {planStep && planStep.unlocks.length > 0 && (
            <div className="rm-ins-sec">
              <div className="rail-h">
                unlocks
                <span className="rail-n">{planStep.unlocks.length}</span>
              </div>
              <div className="rm-unlocks">
                {planStep.unlocks.map((u) => (
                  <button key={u.id} className="rm-unlock" onClick={() => onSelect(u.id)}>
                    {u.label} <ChevronRight size={11} />
                  </button>
                ))}
              </div>
            </div>
          )}

          <a className="rm-ins-graph" href={href('graph', { params: { node: stepId } })}>
            see this concept in the graph <ChevronRight size={12} />
          </a>

          <Passages moments={d.moments} said={d.said} videos={d.videos} />
        </div>
      )}
    </aside>
  );
}

/**
 * The passages to watch for this concept, each a link into the player at the
 * right second. These moments come from *different reels*, so there is no shared
 * timeline to draw a spectrum against — a bar chart here would be a lie about
 * which video a hit belongs to. So it is a list, grouped only by the honesty of
 * `said`: what is literally spoken first, then the strong-passage fallbacks.
 */
function Passages({
  moments,
  said,
  videos,
}: {
  moments: RoadmapStepDetail['moments'];
  said: boolean;
  videos: Record<string, VideoItem>;
}) {
  if (!moments.length) {
    return (
      <div className="rm-ins-none dim">
        This concept has no watchable passage in scope. It is real in the graph — it was mined from
        a column — but nothing in these reels pins it to a moment.
      </div>
    );
  }
  return (
    <div className="rm-passages">
      <div className="rail-h">
        {said ? 'moments that say it' : 'strongest passages'}
        <span className="rail-n">{moments.length}</span>
      </div>
      {!said && (
        <div className="rm-passages-note dim">
          No transcript names this concept, so these are the strongest passages of the reels that
          carry it — the right videos, not a pinpointed instant.
        </div>
      )}
      {moments.map((m) => {
        const ch = channelOf(m.source);
        const v = videos[m.video_key];
        const t = m.t_start;
        return (
          <button
            key={m.id}
            className="rm-passage"
            onClick={() => watch(m.video_key, t ?? undefined, {})}
            title={`${v?.title || m.video_key} · ${fmtT(t)}`}
          >
            <span className="rm-passage-top">
              <span className={chipClass(ch)}>{ch}</span>
              {t !== null && t !== undefined && <span className="rm-passage-t">{fmtT(t)}</span>}
              <Play size={11} className="rm-passage-play" />
            </span>
            {v?.title && <span className="rm-passage-title">{clip(v.title, 60)}</span>}
            {m.text && <span className="rm-passage-text">{clip(m.text, 160)}</span>}
          </button>
        );
      })}
    </div>
  );
}

// A prerequisite's evidence, in a shape a reader can check. Unused as a
// standalone today — the row prints `after a, b` inline — but kept for the
// inspector's next iteration and to document what the numbers mean.
export function PrereqNote({ pr, onOpen }: { pr: RoadmapPrereq; onOpen: (id: string) => void }) {
  return (
    <button className="rm-prereq" onClick={() => onOpen(pr.id)}>
      <Clock size={11} />
      <span className="rm-prereq-l">{pr.label}</span>
      <span className="rm-prereq-n" title="P(this concept | prerequisite) vs the reverse">
        {fmtPct(pr.p_forward, 1)} → {fmtPct(pr.p_back, 1)}
      </span>
    </button>
  );
}

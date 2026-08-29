/**
 * views/Engine.tsx — the engine room.
 *
 * Two questions, side by side, because on this machine they have different
 * answers and the gap between them is the whole story:
 *
 *   - **What is scheduled?** The queue — pending, running, done, failed — read
 *     from `jobs.db` through `/api/engine/jobs`. This is what the single worker
 *     thread is actually doing right now.
 *   - **What is possible?** The catalogue — every pass the pipeline declares,
 *     annotated by the server with whether *this* GPU can host it. A 6 GB model
 *     on a 4.9 GB card is `held`, with the shortfall spelled out, and that is
 *     the reason the queue's `unrunnable` count is what it is.
 *
 * The machine facts on the left are measured, never assumed (`sizing/resources`
 * shells out to the card), so when a pass is refused the number that refused it
 * is right there to be checked. The worker controls flip a daemon-thread flag
 * and re-read the queue within one round-trip rather than waiting on the poll —
 * a button that does not visibly land reads as broken even when it worked.
 *
 * Live telemetry (queue depth, worker state, VRAM, disk) comes from the store's
 * poll, not this view's own timers. The job list is the one thing polled here,
 * because a table of forty rows is not worth pushing through the status strip.
 */

import { useEffect, useMemo, useState } from 'react';
import { Cpu, HardDrive, Network, Pause, Play, RotateCcw, Zap } from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { go, href } from '../lib/router';
import {
  getComponents,
  getEngineJobs,
  pauseEngine,
  resumeEngine,
  startEngine,
} from '../lib/api';
import { refreshHost, refreshTelemetry, useDisk, useEngine, useHost } from '../lib/store';
import { useFetch, type FetchState } from '../lib/useFetch';
import { clip, fmtAgo, fmtBytes, fmtCount, fmtDate } from '../lib/format';
import type { ComponentCatalogue, ComponentRow, DiskUsage, EngineJob, HostFacts } from '../types';
import { CHANNELS } from '../types';

/** VRAM and RAM come off the probe in MiB; every other size is bytes. */
const fmtMB = (mb: number | null | undefined) =>
  mb === null || mb === undefined ? '—' : fmtBytes(mb * 1024 * 1024, 1);

// Seven states, five of them terminal, and the labels are not interchangeable.
// `done` and `skipped` are both successful outcomes — a reel with no audio track
// is not a defect — while `failed` is work that should have happened and did
// not. Showing them as one colour is what made the old two-state queue useless
// as a report: a green wall of "completed" that included every pass that had
// quietly declined.
const JOB_STATE: Record<string, { label: string; cls: string }> = {
  pending: { label: 'pending', cls: 'js-pending' },
  running: { label: 'running', cls: 'js-running' },
  completed: { label: 'done', cls: 'js-done' },
  skipped: { label: 'skipped', cls: 'js-skipped' },
  deferred: { label: 'later', cls: 'js-deferred' },
  failed: { label: 'failed', cls: 'js-failed' },
  unrunnable: { label: 'held', cls: 'js-held' },
};

const FILTERS: Array<{ key: string; label: string }> = [
  { key: '', label: 'all' },
  { key: 'pending', label: 'pending' },
  { key: 'running', label: 'running' },
  { key: 'completed', label: 'done' },
  { key: 'skipped', label: 'skipped' },
  { key: 'failed', label: 'failed' },
  { key: 'unrunnable', label: 'held' },
];

const isChannel = (s: string): boolean => (CHANNELS as string[]).includes(s);

export default function EngineView({ route }: ViewProps) {
  const engine = useEngine();
  const host = useHost();
  const disk = useDisk();

  const stateFilter = route.params.get('state') || '';
  const [busy, setBusy] = useState(false);

  const jobs = useFetch(
    (signal) => getEngineJobs(stateFilter || undefined, 200, signal),
    [stateFilter]
  );
  const cat = useFetch((signal) => getComponents(false, signal), []);

  // The queue's live rows are the only thing polled from inside a view — the
  // store already ticks the *counts* every three seconds, and reloading the
  // full table on that same beat would refetch forty rows to change nothing
  // most of the time. Five seconds keeps a running job visibly advancing.
  useEffect(() => {
    const id = window.setInterval(() => jobs.reload(), 5000);
    return () => window.clearInterval(id);
  }, [jobs.reload]);

  const byId = useMemo(() => {
    const m = new Map<string, ComponentRow>();
    for (const c of cat.data?.components || []) m.set(c.id, c);
    return m;
  }, [cat.data]);

  // Catalogue order is already stage order, and a Map keeps insertion order, so
  // grouping here yields structure → signal → … without a second sort.
  const stages = useMemo(() => {
    const groups = new Map<string, ComponentRow[]>();
    for (const c of cat.data?.components || []) {
      const arr = groups.get(c.stage_name);
      if (arr) arr.push(c);
      else groups.set(c.stage_name, [c]);
    }
    return [...groups.entries()];
  }, [cat.data]);

  const worker: 'running' | 'paused' | 'stopped' | 'unknown' = !engine
    ? 'unknown'
    : engine.running_worker
      ? engine.paused
        ? 'paused'
        : 'running'
      : 'stopped';

  async function act(fn: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    try {
      await fn();
      await refreshTelemetry();
      jobs.reload();
    } catch {
      // A failed control call means the server stopped answering; the status
      // strip already says so from the poll. Nothing useful to add here.
    } finally {
      setBusy(false);
    }
  }

  async function refreshMachine() {
    if (busy) return;
    setBusy(true);
    try {
      await refreshHost(); // re-probes the card server-side
      cat.reload(); // now reads the fresh VRAM ceiling for its held/ready flags
    } finally {
      setBusy(false);
    }
  }

  const cur = engine?.current_job || null;

  return (
    <div className="view view-split eng-view">
      <div className="view-bar">
        <Cpu size={14} className="dim" />
        <strong>Engine</strong>
        <WorkerPill state={worker} />
        <span className="dim">
          {engine
            ? `${fmtCount(engine.pending)} queued · ${fmtCount(engine.running)} running`
            : 'reading the queue…'}
        </span>
        <span className="spacer" />
        <div className="eng-controls">
          {worker === 'running' ? (
            <button className="btn btn-sm" disabled={busy} onClick={() => act(pauseEngine)}>
              <Pause size={13} /> Pause
            </button>
          ) : (
            <button
              className="btn btn-sm btn-primary"
              disabled={busy || worker === 'unknown'}
              onClick={() => act(worker === 'paused' ? resumeEngine : startEngine)}
            >
              <Play size={13} /> {worker === 'paused' ? 'Resume' : 'Start'}
            </button>
          )}
        </div>
      </div>

      <div className="split">
        {/* ── The machine, measured ── */}
        <aside className="rail rail-left" aria-label="This machine">
          <div className="rail-head">
            This machine
            <button
              className="btn-icon"
              disabled={busy}
              onClick={refreshMachine}
              title="re-measure the GPU — free VRAM moves as models load and drop"
            >
              <RotateCcw size={12} />
            </button>
          </div>
          <div className="rail-body eng-rail">
            <MachinePanel host={host} />
            <DiskPanel disk={disk} />
            <SystemsSummary cat={cat.data} />
          </div>
        </aside>

        {/* ── The queue, live ── */}
        <div className="split-main eng-main">
          <div className="stat-row eng-counts">
            <Count n={engine?.pending} label="pending" state="" active={stateFilter} />
            <Count n={engine?.running} label="running" state="running" active={stateFilter} />
            <Count n={engine?.completed} label="done" state="completed" active={stateFilter} />
            <Count n={engine?.skipped} label="skipped" state="skipped" active={stateFilter} />
            <Count n={engine?.failed} label="failed" state="failed" active={stateFilter} />
            <Count n={engine?.unrunnable} label="held" state="unrunnable" active={stateFilter} />
          </div>

          {cur && (
            <div className="eng-current">
              <Zap size={14} className="js-running-fg" />
              <span className="dim">running now</span>
              <a className="font-mono" href={href('watch', { key: cur.video_key })}>
                {shortKey(cur.video_key)}
              </a>
              <span className="eng-cur-comp">
                {byId.get(cur.component_id)?.title || cur.component_id}
              </span>
              {/* The pass's own live line — "decoding 1,412 of 2,280 frames".
                  Worth the width: these are ffmpeg passes over whole videos, and
                  without it a ninety-second decode is indistinguishable from a
                  hang. */}
              {cur.detail && <span className="eng-cur-detail dim">{cur.detail}</span>}
              <span className="spacer" />
              <span className="dim">{fmtAgo(cur.started_at ?? cur.created_at)}</span>
            </div>
          )}

          <div className="eng-sub">
            <div className="segmented">
              {FILTERS.map((f) => (
                <button
                  key={f.key || 'all'}
                  className={f.key === stateFilter ? 'on' : ''}
                  onClick={() => go('engine', { params: { state: f.key } })}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <span className="spacer" />
            <span className="dim">
              {jobs.data ? `${fmtCount(jobs.data.length)} shown` : ''}
            </span>
          </div>

          <JobTable jobs={jobs} byId={byId} />

          <div className="section-h eng-pipe-h">
            <Network size={13} className="dim" />
            <h2>Pipeline</h2>
            {cat.data && (
              <span className="count">
                {fmtCount(cat.data.runnable)} of {fmtCount(cat.data.total)} runnable here
                {cat.data.blocked ? ` · ${fmtCount(cat.data.blocked)} held` : ''}
                {cat.data.measured ? '' : ' · machine not measured'}
              </span>
            )}
          </div>

          {cat.error ? (
            <div className="state-box err">
              <div className="head">Could not read the catalogue</div>
              <div>{cat.error}</div>
            </div>
          ) : cat.first && cat.loading ? (
            <div className="sys-list">
              {Array.from({ length: 8 }, (_, i) => (
                <div className="skel" style={{ height: 30, margin: '3px 0' }} key={i} />
              ))}
            </div>
          ) : (
            stages.map(([stage, comps]) => (
              <div className="sys-stage" key={stage}>
                <div className="sys-stage-h">
                  <span>{stage}</span>
                  <span className="dim">
                    {fmtCount(comps.filter((c) => !c.unrunnable).length)}/{fmtCount(comps.length)}
                  </span>
                </div>
                <div className="sys-list">
                  {comps.map((c) => (
                    <SysRow key={c.id} c={c} />
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function WorkerPill({ state }: { state: 'running' | 'paused' | 'stopped' | 'unknown' }) {
  const label =
    state === 'running'
      ? 'worker running'
      : state === 'paused'
        ? 'worker paused'
        : state === 'stopped'
          ? 'worker stopped'
          : 'worker…';
  return <span className={`eng-pill eng-pill-${state}`}>{label}</span>;
}

function Count({
  n,
  label,
  state,
  active,
}: {
  n: number | null | undefined;
  label: string;
  state: string;
  active: string;
}) {
  return (
    <a
      className={`stat eng-stat${state === active ? ' is-active' : ''}`}
      href={href('engine', { params: { state } })}
    >
      <span className="n">{n === null || n === undefined ? '—' : fmtCount(n)}</span>
      <span className="l">{label}</span>
    </a>
  );
}

function JobTable({
  jobs,
  byId,
}: {
  jobs: FetchState<EngineJob[]>;
  byId: Map<string, ComponentRow>;
}) {
  if (jobs.error) {
    return (
      <div className="state-box err">
        <div className="head">Could not read the queue</div>
        <div>{jobs.error}</div>
      </div>
    );
  }
  if (jobs.first && jobs.loading) {
    return (
      <div className="dtable-wrap eng-jobs">
        {Array.from({ length: 6 }, (_, i) => (
          <div className="skel" style={{ height: 26, margin: '3px 0' }} key={i} />
        ))}
      </div>
    );
  }
  const rows = jobs.data || [];
  if (!rows.length) {
    return (
      <div className="state-box eng-empty">
        <div className="head">Nothing in the queue</div>
        <div>
          Passes are enqueued from a video's own page. The worker picks them up in order;
          anything this machine cannot host lands in <em>held</em> with the reason attached.
        </div>
      </div>
    );
  }
  return (
    <div className="dtable-wrap eng-jobs">
      <table className="dtable">
        <thead>
          <tr>
            <th className="ej-state">state</th>
            <th>reel</th>
            <th>pass</th>
            <th className="ej-num">tries</th>
            <th className="ej-num">rows</th>
            <th>when</th>
            <th>detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((j) => {
            const comp = byId.get(j.component_id);
            const st = JOB_STATE[j.state] || { label: j.state, cls: 'js-pending' };
            const done = j.finished_at && j.started_at ? j.finished_at - j.started_at : null;
            return (
              <tr key={j.job_id}>
                <td className="ej-state">
                  <span className={`jchip ${st.cls}`}>{st.label}</span>
                </td>
                <td>
                  <a className="ej-key" href={href('watch', { key: j.video_key })}>
                    {shortKey(j.video_key)}
                  </a>
                </td>
                <td>
                  {comp?.channel && isChannel(comp.channel) ? (
                    <span className={`chip-channel chip-${comp.channel}`}>{comp.channel}</span>
                  ) : null}{' '}
                  <span title={comp?.summary || j.component_id}>
                    {comp?.title || j.component_id}
                  </span>
                </td>
                <td className="ej-num">{j.attempts ?? 0}</td>
                {/* Evidence rows the pass produced. Blank rather than 0 before it
                    has run: an empty cell is "not yet", a 0 is "ran and measured
                    nothing", and those are different facts about a reel. */}
                <td className="ej-num dim" title={j.shard ? fileName(j.shard) : ''}>
                  {j.rows === null || j.rows === undefined ? '' : fmtCount(j.rows)}
                </td>
                <td
                  className="dim"
                  title={fmtDate(j.finished_at ?? j.started_at ?? j.created_at)}
                >
                  {fmtAgo(j.finished_at ?? j.started_at ?? j.created_at)}
                  {done !== null ? ` · ${done.toFixed(1)}s` : ''}
                </td>
                <JobDetail job={j} />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * A job's `detail` cell: the reason first, then what the pass found.
 *
 * `reason` rather than `error`, because four of the five terminal states are not
 * errors — *"this reel has no audio track"* is a correct outcome — and the
 * server sends the same string under both names precisely so this column can
 * stop calling every one of them an error.
 *
 * The notes are the reason this column is worth its width. A row that says
 * *done · 4 rows* proves the queue ran; a row that says *3 shots · asl 2.13 ·
 * metronomic* proves it measured something, and it is the only place in the app
 * where a pass's own findings are visible without opening the reel.
 */
function JobDetail({ job }: { job: EngineJob }) {
  const reason = job.reason || job.error || '';
  const notes = job.notes
    ? Object.entries(job.notes)
        .filter(([, v]) => v !== null && v !== undefined && v !== '')
        .map(([k, v]) => `${k} ${typeof v === 'number' ? fmtNum(v) : String(v)}`)
    : [];
  const full = [reason, ...notes].filter(Boolean).join(' · ');
  return (
    <td className="ej-detail" title={full}>
      {reason && <span className="ej-reason">{clip(reason, 90)}</span>}
      {!reason && notes.length ? <span className="dim">{clip(notes.join(' · '), 90)}</span> : null}
    </td>
  );
}

/** Enough precision to read, not enough to pretend. `asl 2.13`, not `2.1333`. */
const fmtNum = (n: number) =>
  Number.isInteger(n) ? fmtCount(n) : n.toFixed(Math.abs(n) < 1 ? 3 : 2);

/** The shard's own name, without the bucket directory above it. */
const fileName = (p: string) => p.split(/[\\/]/).pop() || p;

function MachinePanel({ host }: { host: HostFacts | null }) {
  if (!host) {
    return <div className="skel" style={{ height: 120 }} />;
  }
  const gpuNames = host.gpus?.length
    ? [...new Set(host.gpus.map((g) => g.name))].join(', ')
    : 'CPU only';
  const cc = host.compute_capability;
  const usedVram =
    host.vram_total_mb && host.vram_free_mb != null ? host.vram_total_mb - host.vram_free_mb : null;

  return (
    <div className="panel eng-panel">
      <div className="panel-h">
        <Cpu size={13} /> Hardware
      </div>
      <dl className="kv">
        <dt>GPU</dt>
        <dd>{gpuNames}</dd>
        {host.gpu_count > 0 && (
          <>
            <dt>VRAM</dt>
            <dd>
              {fmtMB(host.vram_free_mb)} free of {fmtMB(host.vram_total_mb)}
            </dd>
            <dt>usable</dt>
            <dd title="free VRAM on the smallest card, minus a 1 GB headroom — what one model must fit inside">
              {fmtMB(host.usable_vram_mb)}
            </dd>
          </>
        )}
        <dt>RAM</dt>
        <dd>
          {host.ram_known
            ? `${fmtMB(host.ram_available_mb)} free of ${fmtMB(host.ram_total_mb)}`
            : 'not measured'}
        </dd>
        <dt>CPU</dt>
        <dd>{fmtCount(host.cpus)} cores</dd>
        {cc ? (
          <>
            <dt>compute</dt>
            <dd>
              sm_{cc}
              {host.dtype ? ` · ${host.dtype}` : ''}
              {host.flash_attention_2 ? ' · flash-attn' : ''}
            </dd>
          </>
        ) : null}
      </dl>
      {host.gpu_count > 0 && usedVram !== null && (
        <div className="meter" title={`${fmtMB(usedVram)} of ${fmtMB(host.vram_total_mb)} in use`}>
          <div
            className="meter-fill"
            style={{ width: `${Math.min(100, (usedVram / (host.vram_total_mb || 1)) * 100)}%` }}
          />
        </div>
      )}
    </div>
  );
}

function DiskPanel({ disk }: { disk: DiskUsage | null }) {
  if (!disk) {
    return <div className="skel" style={{ height: 90 }} />;
  }
  const parts = [
    { label: 'videos', bytes: disk.video_bytes, cls: 'seg-video' },
    { label: 'proxies', bytes: disk.proxy_bytes, cls: 'seg-proxy' },
    { label: 'derived', bytes: disk.derived_bytes, cls: 'seg-derived' },
    { label: 'models', bytes: disk.model_bytes, cls: 'seg-model' },
    { label: 'db', bytes: disk.db_bytes, cls: 'seg-db' },
  ];
  const used = parts.reduce((s, p) => s + (p.bytes || 0), 0);
  const span = used + (disk.free_bytes || 0) || 1;

  return (
    <div className="panel eng-panel">
      <div className="panel-h">
        <HardDrive size={13} /> Disk
        {disk.below_floor && <span className="eng-floor">below floor</span>}
      </div>
      <div className="eng-disk-free">
        <strong>{fmtBytes(disk.free_bytes)}</strong> <span className="dim">free</span>
        {disk.free_floor_gb != null && (
          <span className="dim"> · floor {fmtCount(disk.free_floor_gb)} GB</span>
        )}
      </div>
      <div className="meter meter-seg" title={`${fmtBytes(used)} used`}>
        {parts.map((p) =>
          p.bytes ? (
            <div
              key={p.label}
              className={`meter-fill ${p.cls}`}
              style={{ width: `${(p.bytes / span) * 100}%` }}
              title={`${p.label}: ${fmtBytes(p.bytes)}`}
            />
          ) : null
        )}
      </div>
      <div className="eng-disk-legend">
        {parts
          .filter((p) => p.bytes)
          .map((p) => (
            <span key={p.label} className="dim">
              <i className={`dot ${p.cls}`} /> {p.label} {fmtBytes(p.bytes)}
            </span>
          ))}
      </div>
    </div>
  );
}

function SystemsSummary({ cat }: { cat: ComponentCatalogue | null }) {
  if (!cat) return null;
  return (
    <div className="panel eng-panel">
      <div className="panel-h">
        <Network size={13} /> Systems
      </div>
      <div className="stat-row eng-sys-row">
        <div className="stat">
          <span className="n">{fmtCount(cat.runnable)}</span>
          <span className="l">runnable</span>
        </div>
        <div className="stat">
          <span className="n">{fmtCount(cat.blocked)}</span>
          <span className="l">held</span>
        </div>
        {/* How many passes have an implementation here at all. The gap between
            this and `runnable` is what the laptop is waiting on: code, not
            hardware. Without it the tab cannot distinguish "held for a card it
            does not have" from "nobody has written this yet". */}
        {cat.runners !== undefined && (
          <div className="stat" title="passes with a runner on this machine">
            <span className="n">{fmtCount(cat.runners)}</span>
            <span className="l">local</span>
          </div>
        )}
      </div>
      {!cat.measured && (
        <div className="view-hint eng-hint">
          The machine could not be probed, so anything needing a GPU is listed as held on that
          basis rather than on a reading. The {fmtCount(cat.runners ?? 0)} passes that run on the
          CPU here are unaffected. Hit refresh once a card is visible.
        </div>
      )}
    </div>
  );
}

function SysRow({ c }: { c: ComponentRow }) {
  // Three states, not two. A pass with no runner is waiting on code; a pass with
  // a runner and a reason is waiting on a card or a library; a pass with a runner
  // and no reason runs today. The old row collapsed the first two into `held`,
  // which read as "your machine is too small" for twenty passes whose real
  // status is "not written yet".
  const waiting = c.runner === false;
  // The chip on the right already says `no runner`. The server puts that first
  // in the reason too — it has to, because the job table and the API show the
  // sentence with no chip beside it — so the row drops the half it is about to
  // repeat and keeps the clause that says something new.
  const why = (c.reason || '').replace(/^no runner on this machine yet(\s·\s)?/, '');
  return (
    <div className={`sys-row${c.unrunnable ? ' is-held' : ''}`} title={c.summary}>
      <span
        className="sys-dot"
        data-on={c.default_on ? 'y' : 'n'}
        title={c.default_on ? 'on by default' : 'off by default'}
      />
      <span className="sys-title">{c.title}</span>
      {c.channel && isChannel(c.channel) && (
        <span className={`chip-channel chip-${c.channel}`}>{c.channel}</span>
      )}
      <span className="spacer" />
      {/* The reason, in the row rather than only in a tooltip — a hover is not
          discoverable, and "needs 6144 MB on one card, 0 MB usable" is the
          sentence that makes the held state actionable. Clipped generously and
          left to the ellipsis in CSS below that: at this window it fits whole,
          and at a narrow one the row truncates rather than reflows. */}
      {c.unrunnable && why && <span className="sys-why dim">{clip(why, 88)}</span>}
      <span className="sys-dev">{c.device === 'gpu' ? `GPU ${fmtMB(c.vram_mb)}` : 'CPU'}</span>
      {c.unrunnable ? (
        <span className={waiting ? 'sys-wait' : 'sys-held'} title={c.reason || 'cannot run here'}>
          {waiting ? 'no runner' : 'held'}
        </span>
      ) : (
        <span className="sys-ok">ready</span>
      )}
    </div>
  );
}

/** A content key is a long hex hash or a Telegram msg id — show a handle, not a wall. */
function shortKey(key: string): string {
  if (/^\d+$/.test(key)) return key;
  const bare = key.replace(/^loc_/, '');
  return `${key.startsWith('loc_') ? 'loc·' : ''}${bare.slice(0, 8)}`;
}

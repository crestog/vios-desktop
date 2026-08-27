/**
 * views/Capture.tsx — where the archive comes from.
 *
 * This is the only screen in the app that reaches the public internet, and
 * everything about it is shaped by one fact: **a full capture is a week long.**
 * Six thousand reels at a deliberate ninety-second pace is not a progress bar
 * you watch, it is a process you check on — so the screen is built to answer
 * "is it still healthy, and if it stopped, why" in one glance, and to make the
 * two irreversible-feeling actions (a channel scan, a ledger restore) hard to
 * do by accident.
 *
 * Three things here are not decoration:
 *
 *   - **A token goes in and never comes out.** `capture/routes.py` returns
 *     `bot_token_set: true` and no value, anywhere, ever. So the form's fields
 *     are write-only: they start blank, blank means "leave it alone", and the
 *     panel beside them reports presence and *origin* instead. Rendering a
 *     stored token back into an input would put it in every screenshot of this
 *     tab.
 *   - **`failed` and `unavailable` are not the same thing.** A failed row comes
 *     back on its own — there is a retry ladder and six revivals behind it, four
 *     hours apart. An unavailable row is a post Instagram deleted, and no amount
 *     of waiting helps. Showing them as one red number makes a healthy run look
 *     broken, which is exactly how an operator ends up "fixing" a run that was
 *     fine.
 *   - **The pacer is shown, not hidden.** `backoff` above 1 and a non-zero
 *     `hostile_streak` are the archive's early warning that Instagram has
 *     started pushing back; they are the numbers that decide whether to keep
 *     going, and they are on screen rather than in a log file.
 *
 * Polling is this view's own, unlike Engine's: the store ticks mirror and engine
 * telemetry for the status strip, and capture is not in the strip. Two seconds
 * while a run is live, eight when it is not — a paused capture that nobody is
 * watching should not cost 1,800 requests an hour.
 */

import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Check,
  CloudDownload,
  Download,
  FileUp,
  Gauge,
  KeyRound,
  Link2,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Satellite,
  Square,
  Upload,
  X,
} from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { go, href } from '../lib/router';
import {
  capturePreflight,
  captureExportUrl,
  getBackfillStatus,
  getCaptureActivity,
  getCaptureCollections,
  getCaptureFailures,
  getCaptureQueue,
  getCaptureStatus,
  importCaptureFile,
  importCapturePath,
  importCaptureText,
  openUrl,
  pauseCapture,
  requeueCapture,
  rescanCaptureChannel,
  resumeCapture,
  saveCaptureConfig,
  seedCaptureChannel,
  snapshotCapture,
  startBackfill,
  startCapture,
  stopBackfill,
  stopCapture,
  type CaptureConfigFields,
} from '../lib/api';
import { useFetch, type FetchState } from '../lib/useFetch';
import {
  clip, fmtAgo, fmtBytes, fmtCount, fmtDate, fmtDur, fmtIn, fmtPct, plural,
} from '../lib/format';
import type {
  BackfillStatus,
  CaptureCollection,
  CaptureCounts,
  CaptureEvent,
  CaptureFailure,
  CaptureQueueResponse,
  CaptureSettings,
  CaptureStatus,
  PreflightResponse,
} from '../types';

/* ── The six ledger states, in the order a row travels through them ──
   `unavailable` and `skipped` are terminal and *not* failures, which is why
   they get their own colours rather than sharing `failed`'s red.

   `cls` is optional because the leading `all` entry is not a state and never
   reaches a `className`: the counts row filters it out (`.filter((s) => s.key)`),
   the segmented control renders labels, and the two chip sites look a row's own
   state up by key — which is never the empty string. It carried a `cs-all` for
   long enough that the CSS audit reported the class as unstyled; the class was
   the dead thing, not the rule. */
const STATES: Array<{ key: string; label: string; cls?: string; why: string }> = [
  { key: '', label: 'all', why: 'every row in the ledger' },
  { key: 'queued', label: 'queued', cls: 'cs-queued', why: 'waiting its turn' },
  { key: 'fetching', label: 'fetching', cls: 'cs-fetching', why: 'in flight right now' },
  { key: 'uploaded', label: 'captured', cls: 'cs-uploaded', why: 'in the channel — done' },
  {
    key: 'failed',
    label: 'failed',
    cls: 'cs-failed',
    why: 'will be retried on its own — up to six revivals, four hours apart',
  },
  {
    key: 'unavailable',
    label: 'gone',
    cls: 'cs-unavailable',
    why: 'deleted or private on Instagram — waiting will not help',
  },
  { key: 'skipped', label: 'skipped', cls: 'cs-skipped', why: 'excluded by a collection filter' },
];

const TABS: Array<{ key: string; label: string }> = [
  { key: 'queue', label: 'Queue' },
  { key: 'failures', label: 'Failures' },
  { key: 'log', label: 'Activity' },
];

const PAGE = 100;

/** Event kinds worth colouring. Everything else prints in the neutral shade. */
const EVENT_TONE: Record<string, string> = {
  captured: 'ev-good',
  assets: 'ev-good',
  seed: 'ev-good',
  import: 'ev-good',
  restore: 'ev-good',
  snapshot: 'ev-good',
  rebind: 'ev-warn',
  repair: 'ev-warn',
  hostile: 'ev-warn',
  halt: 'ev-warn',
  'asset-note': 'ev-warn',
  'slide-missing': 'ev-warn',
  'upload-failed': 'ev-bad',
  'seed-failed': 'ev-bad',
  'snapshot-failed': 'ev-bad',
  'backfill-failed': 'ev-bad',
  unavailable: 'ev-bad',
  crash: 'ev-bad',
};

const n = (c: CaptureCounts | undefined, k: string) => (c ? c[k] ?? 0 : 0);

export default function CaptureView({ route }: ViewProps) {
  const tab = route.params.get('tab') || 'queue';
  const stateFilter = route.params.get('state') || '';
  const offset = Math.max(0, Number(route.params.get('off') || 0) || 0);

  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);

  const status = useFetch(getCaptureStatus, []);
  const live = status.data?.state === 'running' || status.data?.state === 'stopping';
  const taskRunning = Boolean(status.data?.task?.running);

  // Two rates, one reason: a live run changes every few seconds and an idle one
  // changes when someone presses a button. A background scan counts as live —
  // its progress message is the only feedback a two-minute channel walk gives.
  useEffect(() => {
    const every = live || taskRunning ? 2000 : 8000;
    const id = window.setInterval(() => status.reload(), every);
    return () => window.clearInterval(id);
  }, [status.reload, live, taskRunning]);

  const queue = useFetch(
    (signal) => getCaptureQueue({ state: stateFilter, limit: PAGE, offset }, signal),
    [stateFilter, offset],
    { enabled: tab === 'queue' }
  );
  const failures = useFetch((signal) => getCaptureFailures(200, signal), [], {
    enabled: tab === 'failures',
  });
  const activity = useFetch((signal) => getCaptureActivity(120, signal), [], {
    enabled: tab === 'log',
  });
  const collections = useFetch(getCaptureCollections, []);
  const backfill = useFetch(getBackfillStatus, []);

  // The visible list follows the run, but only the visible one: refetching the
  // failures table every two seconds while watching the queue would triple the
  // request rate to paint nothing.
  const reloadVisible =
    tab === 'queue' ? queue.reload : tab === 'failures' ? failures.reload : activity.reload;
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => reloadVisible(), 6000);
    return () => window.clearInterval(id);
  }, [live, reloadVisible]);

  const bfLive = backfill.data?.state === 'running' || backfill.data?.state === 'stopping';
  useEffect(() => {
    const id = window.setInterval(() => backfill.reload(), bfLive ? 3000 : 20000);
    return () => window.clearInterval(id);
  }, [backfill.reload, bfLive]);

  const st = status.data;
  const counts = st?.counts;
  const settings = st?.settings;
  const ready = Boolean(settings?.bot_token_set && settings?.channel);

  /**
   * The one thing that stops Start, and the same rule the server applies in
   * `Engine._seed_gate`. A ledger that has never read the channel cannot tell
   * "not captured" from "not asked", so capturing against it re-downloads and
   * re-uploads everything already in the channel — a week of Instagram requests
   * and a duplicate of every reel.
   *
   * It is only a *block* when there is nothing the scan could run with. With an
   * API id and hash present, Start scans first and the notice is a heads-up
   * about the two-minute walk, not a refusal.
   */
  const seed = st?.seeded;
  const needsSeed = Boolean(st && !seed?.seeded && n(counts, 'remaining') > 0);
  const canScan = Boolean(settings?.api_credentials_set);
  const seedBlocks = needsSeed && !canScan;

  /**
   * Every button goes through here. Three jobs: one at a time (a double-click on
   * Start must not start twice), the server's own sentence is what gets shown on
   * failure, and the poll is pulled forward so the button visibly lands rather
   * than appearing to do nothing for two seconds.
   */
  async function act(tag: string, fn: () => Promise<unknown>, after?: () => void) {
    if (busy) return;
    setBusy(tag);
    setNotice(null);
    try {
      await fn();
      status.reload();
      after?.();
    } catch (e) {
      setNotice({ tone: 'bad', text: String((e as Error)?.message || e) });
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="view view-split cap-view">
      <div className="view-bar">
        <Satellite size={14} className="dim" />
        <strong>Capture</strong>
        <StatePill state={st?.state} live={live} />
        <span className="dim cap-bar-note">
          {st
            ? `${fmtCount(n(counts, 'uploaded'))} captured · ${fmtCount(
                n(counts, 'remaining')
              )} to go`
            : 'reading the ledger…'}
        </span>
        {st?.waiting_seconds ? (
          <span className="cap-wait" title="the deliberate gap between requests — the whole anti-detection budget">
            waiting {fmtDur(st.waiting_seconds)}
          </span>
        ) : null}
        <span className="spacer" />
        <div className="cap-controls">
          {live ? (
            <>
              <button
                className="btn btn-sm"
                disabled={Boolean(busy)}
                onClick={() => act('pause', pauseCapture)}
              >
                <Pause size={13} /> Pause
              </button>
              <button
                className="btn btn-sm"
                disabled={Boolean(busy)}
                onClick={() => act('stop', stopCapture)}
                title="finish the reel in flight, then stop"
              >
                <Square size={12} /> Stop
              </button>
            </>
          ) : st?.state === 'paused' ? (
            <>
              <button
                className="btn btn-sm btn-primary"
                disabled={Boolean(busy)}
                onClick={() => act('resume', resumeCapture)}
              >
                <Play size={13} /> Resume
              </button>
              <button
                className="btn btn-sm"
                disabled={Boolean(busy)}
                onClick={() => act('stop', stopCapture)}
              >
                <Square size={12} /> Stop
              </button>
            </>
          ) : (
            <button
              className="btn btn-sm btn-primary"
              disabled={Boolean(busy) || !ready || seedBlocks}
              title={
                !ready
                  ? 'set the bot token and channel id first'
                  : seedBlocks
                    ? 'the channel has never been read, and reading it needs the API id and hash'
                    : needsSeed
                      ? 'reads the channel first — a few minutes — then starts capturing what is genuinely missing'
                      : 'scan the channel for what is already there, then start capturing'
              }
              onClick={() => act('start', () => startCapture(true))}
            >
              <Play size={13} /> Start
            </button>
          )}
        </div>
      </div>

      <div className="split">
        <aside className="rail rail-left" aria-label="Capture setup">
          <div className="rail-head">
            Setup
            <button
              className="btn-icon"
              disabled={Boolean(busy)}
              onClick={() => act('preflight', capturePreflight)}
              title="re-read the machine's own status"
            >
              <RotateCcw size={12} />
            </button>
          </div>
          <div className="rail-body cap-rail">
            <CredentialsPanel
              settings={settings}
              busy={busy}
              onSave={(fields, done) =>
                act('config', () => saveCaptureConfig(fields), done)
              }
            />
            <PacerPanel status={st} />
            <PreflightPanel busy={busy} />
            <SourcesPanel
              busy={busy}
              task={st?.task}
              seed={seed}
              onText={(text) => act('import', () => importCaptureText(text))}
              onPath={(path) => act('import', () => importCapturePath(path))}
              onFile={(file) => act('import', () => importCaptureFile(file))}
              onScan={() => act('scan', seedCaptureChannel)}
              onRescan={() => act('rescan', rescanCaptureChannel)}
            />
            <CollectionsPanel data={collections.data?.collections} />
          </div>
        </aside>

        <div className="split-main cap-main">
          {/* Ahead of every other notice, because it is the one that decides
              whether the numbers below mean anything. `remaining` on an unseeded
              ledger is not a queue, it is a list of things nobody has checked. */}
          {needsSeed && (
            <div className={`cap-notice ${seedBlocks ? 'is-bad' : 'is-warn'}`}>
              <AlertTriangle size={13} />
              <span>
                The channel has never been read, so {fmtCount(n(counts, 'remaining'))}{' '}
                {n(counts, 'remaining') === 1 ? 'reel is' : 'reels are'} queued without
                anyone having checked whether {n(counts, 'remaining') === 1 ? 'it is' : 'they are'}{' '}
                already uploaded.{' '}
                {seedBlocks ? (
                  <>
                    Reading it needs the API id and hash as well as the bot token — a bot
                    cannot read channel history without them. Add them under Credentials, or
                    paste the list of links already captured under Sources.
                  </>
                ) : (
                  <>Start reads the channel first; that takes a few minutes and then nothing
                  already there is fetched again.</>
                )}
              </span>
            </div>
          )}

          {notice && (
            <div className={`cap-notice ${notice.tone === 'bad' ? 'is-bad' : 'is-ok'}`}>
              {notice.tone === 'bad' ? <AlertTriangle size={13} /> : <Check size={13} />}
              <span>{notice.text}</span>
              <span className="spacer" />
              <button className="btn-icon" onClick={() => setNotice(null)}>
                <X size={12} />
              </button>
            </div>
          )}

          {status.error && (
            <div className="cap-notice is-bad">
              <AlertTriangle size={13} />
              <span>{status.error}</span>
            </div>
          )}

          {st?.error ? (
            <div className="cap-notice is-bad">
              <AlertTriangle size={13} />
              <span>{st.error}</span>
            </div>
          ) : null}

          <div className="stat-row cap-counts">
            {STATES.filter((s) => s.key).map((s) => (
              <a
                key={s.key}
                className={`stat cap-stat ${s.cls ?? ''}${
                  s.key === stateFilter && tab === 'queue' ? ' is-active' : ''
                }`}
                href={href('capture', { params: { tab: 'queue', state: s.key } })}
                title={s.why}
              >
                <span className="n">{st ? fmtCount(n(counts, s.key)) : '—'}</span>
                <span className="l">{s.label}</span>
              </a>
            ))}
          </div>

          <RunBanner status={st} />
          <TaskBanner task={st?.task} />

          <div className="cap-sub">
            <div className="segmented">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  className={t.key === tab ? 'on' : ''}
                  onClick={() =>
                    go('capture', { params: { tab: t.key, state: stateFilter } })
                  }
                >
                  {t.label}
                </button>
              ))}
            </div>
            {tab === 'queue' && (
              <div className="segmented cap-state-seg">
                {STATES.map((s) => (
                  <button
                    key={s.key || 'all'}
                    className={s.key === stateFilter ? 'on' : ''}
                    title={s.why}
                    onClick={() => go('capture', { params: { tab: 'queue', state: s.key } })}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            )}
            <span className="spacer" />
            {tab === 'failures' && (
              <button
                className="btn btn-sm"
                disabled={Boolean(busy)}
                title="put every failed row back in the queue now, with a fresh attempt ladder — for after the cookies have been refreshed"
                onClick={() => act('requeue', () => requeueCapture('failed'), failures.reload)}
              >
                <RefreshCw size={12} /> Requeue failed
              </button>
            )}
            <a className="btn btn-sm" href={captureExportUrl()} download title="the whole ledger as JSON">
              <Download size={12} /> Export
            </a>
            <button
              className="btn btn-sm"
              disabled={Boolean(busy) || !ready}
              title="push the ledger to the channel now, rather than waiting for the interval"
              onClick={() => act('snapshot', snapshotCapture)}
            >
              <Upload size={12} /> Snapshot
            </button>
          </div>

          {tab === 'queue' && (
            <QueueTable
              queue={queue}
              offset={offset}
              stateFilter={stateFilter}
              currentKey={st?.current?.key}
            />
          )}
          {tab === 'failures' && <FailureTable failures={failures} />}
          {tab === 'log' && <ActivityList activity={activity} />}

          <BackfillCard
            data={backfill.data}
            busy={busy}
            ready={ready}
            onStart={() => act('bf-start', () => startBackfill(0), backfill.reload)}
            onStop={() => act('bf-stop', stopBackfill, backfill.reload)}
          />
        </div>
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════
   Header bits
   ══════════════════════════════════════════════════════════════════════ */

function StatePill({ state, live }: { state?: string; live: boolean }) {
  const s = state || 'unknown';
  const label =
    s === 'running'
      ? 'capturing'
      : s === 'paused'
        ? 'paused'
        : s === 'stopping'
          ? 'stopping'
          : s === 'error'
            ? 'error'
            : s === 'idle'
              ? 'idle'
              : '…';
  return <span className={`cap-pill cap-pill-${s}${live ? ' is-live' : ''}`}>{label}</span>;
}

/**
 * The reel in flight, and the run around it.
 *
 * `sent`/`total` only exist during a multipart upload, so the bar appears for
 * the upload phase and not the fetch — a fetch through yt-dlp reports no byte
 * count this side of completion, and a bar that sat at zero for forty seconds
 * would be worse than no bar.
 */
function RunBanner({ status }: { status?: CaptureStatus | null }) {
  if (!status) return null;
  const cur = status.current || {};
  const session = status.session;
  const has = Boolean(cur.key);
  if (!has && status.state === 'idle') {
    return status.message ? <div className="cap-idle dim">{status.message}</div> : null;
  }
  const pct =
    cur.sent && cur.total ? Math.min(100, (cur.sent / cur.total) * 100) : null;

  return (
    <div className="cap-run">
      <div className="cap-run-head">
        {has ? (
          <>
            <span className={`cap-phase ph-${cur.phase || 'fetching'}`}>
              {cur.phase || 'fetching'}
            </span>
            <span className="font-mono cap-run-key">{cur.key}</span>
            {cur.url ? (
              <button
                className="btn-ghost"
                title={cur.url}
                onClick={() => openUrl(cur.url as string)}
              >
                <Link2 size={11} /> post
              </button>
            ) : null}
            {cur.attempt && cur.attempt > 1 ? (
              <span className="cap-attempt" title="this reel has been tried before">
                attempt {cur.attempt}
              </span>
            ) : null}
            {cur.bytes ? <span className="dim">{fmtBytes(cur.bytes)}</span> : null}
            <span className="spacer" />
            <span className="dim">{cur.started ? fmtAgo(cur.started) : ''}</span>
          </>
        ) : (
          <>
            <span className="dim">{status.message || 'no reel in flight'}</span>
            <span className="spacer" />
          </>
        )}
      </div>

      {pct !== null && (
        <div className="meter cap-up-meter" title={`${fmtBytes(cur.sent)} of ${fmtBytes(cur.total)} uploaded`}>
          <div className="meter-fill" style={{ width: `${pct}%` }} />
        </div>
      )}

      <div className="cap-run-foot">
        <span>
          <strong className="font-mono">{fmtCount(session?.captured)}</strong>{' '}
          <span className="dim">this session</span>
        </span>
        {session?.failed ? (
          <span>
            <strong className="font-mono cap-fail-n">{fmtCount(session.failed)}</strong>{' '}
            <span className="dim">failed</span>
          </span>
        ) : null}
        <span className="dim">·</span>
        <span title="rows finished in the last hour, straight from the ledger">
          <strong className="font-mono">{fmtCount(status.per_hour)}</strong>{' '}
          <span className="dim">per hour</span>
        </span>
        <span className="dim">·</span>
        <span title="at the current pace, over what is left">
          <strong className="font-mono">{status.eta_hours}</strong> <span className="dim">h left</span>
        </span>
        {session?.elapsed ? (
          <>
            <span className="dim">·</span>
            <span className="dim" title={fmtDate(session.started_at)}>
              running {fmtDur(session.elapsed)}
            </span>
          </>
        ) : null}
        {status.hostile_streak > 0 && (
          <span
            className="cap-hostile"
            title="consecutive rate-limit responses from Instagram. The pacer has already slowed down; a streak that keeps climbing means stop for the day."
          >
            <AlertTriangle size={11} /> hostile ×{status.hostile_streak}
          </span>
        )}
      </div>
    </div>
  );
}

/** The single background slot — a channel scan or an import, with its message. */
function TaskBanner({ task }: { task?: CaptureStatus['task'] }) {
  if (!task || (!task.running && !task.error && !task.kind)) return null;
  if (!task.running && !task.error) return null;
  return (
    <div className={`cap-task${task.error ? ' is-bad' : ''}`}>
      {task.running ? (
        <RefreshCw size={13} className="spin" />
      ) : (
        <AlertTriangle size={13} />
      )}
      <span className="cap-task-kind">{task.kind}</span>
      <span className="dim">{task.error || task.message}</span>
      <span className="spacer" />
      {task.at ? <span className="dim">{fmtAgo(task.at)}</span> : null}
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════
   The rail
   ══════════════════════════════════════════════════════════════════════ */

/**
 * Write-only credentials.
 *
 * Every input starts empty and stays empty after a save, because there is
 * nothing to put back in it — the server does not return values. The panel above
 * the form reports what is set and where it came from, which is the question an
 * operator actually has ("I put the token in Kaggle Secrets, did it arrive?").
 */
function CredentialsPanel({
  settings,
  busy,
  onSave,
}: {
  settings?: CaptureSettings;
  busy: string;
  onSave: (fields: CaptureConfigFields, done: () => void) => void;
}) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState<CaptureConfigFields>({});
  const stored = settings?.stored_credentials;
  const fields = stored?.fields || [];

  const set = (k: keyof CaptureConfigFields) => (v: string) =>
    setF((prev) => ({ ...prev, [k]: v }));

  const dirty = Object.values(f).some((v) => String(v ?? '').trim() !== '');

  return (
    <div className="panel cap-panel">
      <div className="panel-h">
        <KeyRound size={13} /> Credentials
        <span className="spacer" />
        <button className="btn-ghost" onClick={() => setOpen((o) => !o)}>
          {open ? 'hide' : 'edit'}
        </button>
      </div>

      <div className="cap-creds">
        {fields.length ? (
          fields.map((c) => (
            <div className="cap-cred" key={c.name} title={c.description}>
              <span className={`cap-dot${c.present ? ' is-on' : ''}`} />
              <span className="cap-cred-n">{c.label}</span>
              <span className="spacer" />
              <span className="dim">{c.present ? c.source || 'set' : 'missing'}</span>
            </div>
          ))
        ) : (
          <>
            <div className="cap-cred">
              <span className={`cap-dot${settings?.bot_token_set ? ' is-on' : ''}`} />
              <span className="cap-cred-n">bot token</span>
              <span className="spacer" />
              <span className="dim">{settings?.bot_token_set ? 'set' : 'missing'}</span>
            </div>
            <div className="cap-cred">
              <span className={`cap-dot${settings?.channel ? ' is-on' : ''}`} />
              <span className="cap-cred-n">channel</span>
              <span className="spacer" />
              <span className="dim font-mono">{settings?.channel ?? 'missing'}</span>
            </div>
            <div className="cap-cred">
              <span className={`cap-dot${settings?.api_credentials_set ? ' is-on' : ''}`} />
              <span className="cap-cred-n">MTProto pair</span>
              <span className="spacer" />
              <span className="dim">{settings?.api_credentials_set ? 'set' : 'missing'}</span>
            </div>
          </>
        )}
        <div className="cap-cred">
          <span className={`cap-dot${settings?.cookies_set ? ' is-on' : ''}`} />
          <span className="cap-cred-n">IG cookies</span>
          <span className="spacer" />
          <span className="dim">
            {settings?.cookies_set ? 'loaded' : 'none — saved reels will fail'}
          </span>
        </div>
      </div>

      {open && (
        <div className="cap-form">
          <p className="cap-form-note">
            Blank means <em>leave it alone</em>. Nothing typed here is ever read back — the
            server reports presence only.
          </p>
          <Field label="bot token" secret value={f.bot_token} onChange={set('bot_token')} />
          <Field
            label="channel id"
            value={f.channel_id as string | undefined}
            onChange={set('channel_id')}
            hint="-100…"
          />
          <Field label="api id" value={f.api_id as string | undefined} onChange={set('api_id')} />
          <Field label="api hash" secret value={f.api_hash} onChange={set('api_hash')} />
          <label className="cap-lbl">
            Instagram cookies
            <textarea
              className="input-text cap-ta"
              rows={3}
              placeholder="paste the cookies.txt contents"
              value={f.cookies_text || ''}
              onChange={(e) => set('cookies_text')(e.target.value)}
            />
            <span className="dim">
              Written to scratch at mode 600 for the length of the process, removed on stop.
              Never snapshotted.
            </span>
          </label>
          <div className="cap-form-row">
            <button
              className="btn btn-sm btn-primary"
              disabled={Boolean(busy) || !dirty}
              onClick={() => onSave(f, () => setF({}))}
            >
              {busy === 'config' ? 'saving…' : 'Save'}
            </button>
            <button className="btn btn-sm" disabled={Boolean(busy)} onClick={() => setF({})}>
              Clear
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  secret,
  hint,
}: {
  label: string;
  value?: string;
  onChange: (v: string) => void;
  secret?: boolean;
  hint?: string;
}) {
  return (
    <label className="cap-lbl">
      {label}
      <input
        className="input-text"
        type={secret ? 'password' : 'text'}
        autoComplete="off"
        spellCheck={false}
        placeholder={hint || (secret ? '••••••••' : '')}
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

/**
 * The rate limiter, showing its work — and editable, because the pace is the
 * one setting that gets changed mid-week.
 *
 * `backoff` is the number to watch: 1.0 is the configured pace, anything above
 * it means the pacer has already slowed itself down in response to a refusal.
 */
function PacerPanel({ status }: { status?: CaptureStatus | null }) {
  const p = status?.pacer;
  if (!p) return <div className="skel" style={{ height: 96 }} />;
  const s = status?.settings;
  return (
    <div className="panel cap-panel">
      <div className="panel-h">
        <Gauge size={13} /> Pace
        <span className="spacer" />
        <span className={`cap-profile prof-${p.profile}`}>{p.profile}</span>
      </div>
      <dl className="kv cap-kv">
        <dt>gap</dt>
        <dd title="seconds it aims for between requests, and the floor it will not go under">
          {p.target}s <span className="dim">/ floor {p.floor}s</span>
        </dd>
        <dt>backoff</dt>
        <dd className={p.backoff > 1 ? 'cap-hot' : ''} title="1.0 is the configured pace; above it means Instagram pushed back and the pacer slowed down on its own">
          ×{p.backoff}
        </dd>
        <dt>rate</dt>
        <dd>{p.requests_per_minute}/min</dd>
        {p.breaks && (
          <>
            <dt>next break</dt>
            <dd>in {plural(p.until_break, 'reel')}</dd>
          </>
        )}
        <dt>quiet hours</dt>
        <dd>{p.quiet_hours ? 'on' : 'off'}</dd>
        {s ? (
          <>
            <dt>attempts</dt>
            <dd title="tries before a row is parked and spends one of its six revivals">
              {s.max_attempts}
            </dd>
            <dt>fallback</dt>
            <dd>{s.gallery_dl_fallback ? 'gallery-dl on' : 'yt-dlp only'}</dd>
          </>
        ) : null}
      </dl>
      {s?.skip_collections?.length ? (
        <div className="cap-skips">
          <span className="dim">skipping</span>{' '}
          {s.skip_collections.map((c) => (
            <span className="cap-skip" key={c}>
              {c}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Preflight: everything that could stop a week-long run, checked in one call.
 *
 * Deliberately not run on mount. It probes Telegram and stats the disk, and a
 * view that fired it on every navigation would hit the Bot API each time the
 * operator glanced at this tab.
 */
function PreflightPanel({ busy }: { busy: string }) {
  const [res, setRes] = useState<PreflightResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function run() {
    setRunning(true);
    setErr(null);
    try {
      setRes(await capturePreflight());
    } catch (e) {
      setErr(String((e as Error)?.message || e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="panel cap-panel">
      <div className="panel-h">
        <Check size={13} /> Readiness
        <span className="spacer" />
        <button className="btn-ghost" disabled={running || Boolean(busy)} onClick={run}>
          {running ? 'checking…' : 'check'}
        </button>
      </div>
      {err && <div className="cap-pf-err">{err}</div>}
      {!res && !err && (
        <p className="dim cap-pf-hint">
          Checks yt-dlp, ffmpeg, the Telegram pair, the cookie jar, free disk and the queue.
          A missing cookie file found at reel one costs an hour; found here it costs a sentence.
        </p>
      )}
      {res && (
        <>
          <div className={`cap-pf-verdict${res.ready ? ' is-ok' : ' is-bad'}`}>
            {res.ready ? 'ready to run' : `blocked: ${res.blocking.join(', ')}`}
          </div>
          <div className="cap-pf-list">
            {res.checks.map((c) => (
              <div className={`cap-pf${c.ok ? '' : ' is-bad'}`} key={c.name}>
                {c.ok ? <Check size={11} /> : <X size={11} />}
                <span className="cap-pf-n">{c.name}</span>
                <span className="cap-pf-d" title={c.detail}>
                  {c.detail}
                </span>
              </div>
            ))}
          </div>
          <div className="dim cap-pf-eta">
            {fmtCount(n(res.counts, 'remaining'))} to capture · about {res.eta_hours} h at
            this pace
          </div>
        </>
      )}
    </div>
  );
}

/**
 * The four ways rows get into the ledger.
 *
 * The channel scan is first because it is the one that must be run first: it
 * teaches the ledger what the channel already holds, and the whole no-double-work
 * guarantee depends on it. Rescan is beside it and deliberately plain — it only
 * clears a watermark, and the next scan does the reading.
 */
function SourcesPanel({
  busy,
  task,
  seed,
  onText,
  onPath,
  onFile,
  onScan,
  onRescan,
}: {
  busy: string;
  task?: CaptureStatus['task'];
  seed?: CaptureStatus['seeded'];
  onText: (t: string) => void;
  onPath: (p: string) => void;
  onFile: (f: File) => void;
  onScan: () => void;
  onRescan: () => void;
}) {
  const [text, setText] = useState('');
  const [path, setPath] = useState('');
  const file = useRef<HTMLInputElement>(null);
  const blocked = Boolean(busy) || Boolean(task?.running);

  return (
    <div className="panel cap-panel">
      <div className="panel-h">
        <FileUp size={13} /> Sources
      </div>

      <div className="cap-src-row">
        <button
          className="btn btn-sm"
          disabled={blocked}
          title="read the channel and adopt everything already uploaded, so nothing is captured twice"
          onClick={onScan}
        >
          <CloudDownload size={12} /> Scan channel
        </button>
        <button
          className="btn btn-sm"
          disabled={blocked}
          title="forget the scan watermark — the next scan re-reads the channel from message 1"
          onClick={onRescan}
        >
          Rescan
        </button>
      </div>

      {/* Under the two buttons that change it, because "never" is the answer
          that explains why Start refused and it belongs next to the fix. */}
      <div className="cap-note">
        {!seed || !seed.seeded ? (
          'the channel has never been read'
        ) : seed.how === 'pasted' ? (
          <>
            adopted from a pasted list · no message ids, so a scan is still worth running
          </>
        ) : (
          <>
            read to message {fmtCount(seed.scanned_to)} · {fmtCount(seed.in_channel)}{' '}
            {seed.in_channel === 1 ? 'video' : 'videos'} in the channel
            {seed.at ? <> · {fmtAgo(seed.at)}</> : null}
          </>
        )}
      </div>

      <label className="cap-lbl">
        paste links
        <textarea
          className="input-text cap-ta"
          rows={3}
          placeholder="https://www.instagram.com/reel/…"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      <button
        className="btn btn-sm"
        disabled={blocked || !text.trim()}
        onClick={() => {
          onText(text);
          setText('');
        }}
      >
        Add links
      </button>

      <label className="cap-lbl">
        export file on this machine
        <input
          className="input-text"
          placeholder="C:\…\instagram_export.zip"
          value={path}
          onChange={(e) => setPath(e.target.value)}
        />
      </label>
      <div className="cap-src-row">
        <button
          className="btn btn-sm"
          disabled={blocked || !path.trim()}
          onClick={() => {
            onPath(path);
            setPath('');
          }}
        >
          Import path
        </button>
        <button
          className="btn btn-sm"
          disabled={blocked}
          onClick={() => file.current?.click()}
        >
          Upload…
        </button>
        <input
          ref={file}
          type="file"
          hidden
          accept=".zip,.json,.md,.txt,.html"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onFile(f);
            e.target.value = '';
          }}
        />
      </div>
      <p className="dim cap-src-note">
        Only the JSON export is needed — the media in it gets re-fetched anyway, and the
        path box skips a 20 MB round trip through the browser.
      </p>
    </div>
  );
}

function CollectionsPanel({ data }: { data?: CaptureCollection[] }) {
  if (!data?.length) return null;
  return (
    <div className="panel cap-panel">
      <div className="panel-h">Collections</div>
      <div className="cap-colls">
        {data.map((c) => (
          <div className="cap-coll" key={c.name}>
            <span className="cap-coll-n" title={c.name}>
              {c.name}
            </span>
            <span className="spacer" />
            <span className="dim font-mono">
              {fmtCount(c.done)}/{fmtCount(c.n)}
            </span>
            <span className="cap-coll-bar">
              <span style={{ width: `${c.n ? (c.done / c.n) * 100 : 0}%` }} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════
   The three lists
   ══════════════════════════════════════════════════════════════════════ */

function QueueTable({
  queue,
  offset,
  stateFilter,
  currentKey,
}: {
  queue: FetchState<CaptureQueueResponse>;
  offset: number;
  stateFilter: string;
  currentKey?: string;
}) {
  if (queue.error) {
    return (
      <div className="state-box err">
        <div className="head">Could not read the ledger</div>
        <div>{queue.error}</div>
      </div>
    );
  }
  if (queue.first && queue.loading) {
    return (
      <div className="dtable-wrap cap-rows">
        {Array.from({ length: 8 }, (_, i) => (
          <div className="skel" style={{ height: 26, margin: '3px 0' }} key={i} />
        ))}
      </div>
    );
  }
  const rows = queue.data?.items || [];
  const total = stateFilter
    ? n(queue.data?.counts, stateFilter)
    : n(queue.data?.counts, 'total');

  if (!rows.length) {
    return (
      <div className="state-box">
        <div className="head">
          {stateFilter ? `Nothing ${stateFilter}` : 'The ledger is empty'}
        </div>
        <div>
          {stateFilter
            ? 'Which is usually the answer you want.'
            : 'Scan the channel to adopt what is already uploaded, then import an export or paste some links.'}
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="dtable-wrap cap-rows">
        <table className="dtable">
          <thead>
            <tr>
              <th className="cq-state">state</th>
              <th>key</th>
              <th>uploader</th>
              <th className="cq-num">dur</th>
              <th className="cq-num">size</th>
              <th className="cq-num">likes</th>
              <th className="cq-num">msg</th>
              <th>when</th>
              <th>detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const meta = STATES.find((s) => s.key === r.state);
              return (
                <tr key={r.key} className={r.key === currentKey ? 'is-current' : ''}>
                  <td className="cq-state">
                    <span className={`cchip ${meta?.cls || 'cs-queued'}`} title={meta?.why}>
                      {meta?.label || r.state}
                    </span>
                  </td>
                  <td>
                    {r.msg_id ? (
                      // The ledger key *is* the archive key — `normalize_key`
                      // returns a shortcode whole (atlas/reflect.py:589), so a
                      // captured row links straight into the player. Rows with
                      // no message id are not in the channel yet and can only
                      // point back at Instagram.
                      <a
                        className="cq-key"
                        href={href('watch', { key: r.key })}
                        title="open it in the player — it is in the channel"
                      >
                        {r.key}
                      </a>
                    ) : (
                      <button
                        className="cq-key cq-key-ext"
                        title={r.url}
                        onClick={() => openUrl(r.url)}
                      >
                        {r.key}
                      </button>
                    )}
                  </td>
                  <td className="cq-up" title={r.uploader || ''}>
                    {r.uploader || ''}
                  </td>
                  <td className="cq-num">{r.duration ? fmtDur(r.duration) : ''}</td>
                  <td className="cq-num">{r.file_size ? fmtBytes(r.file_size) : ''}</td>
                  <td className="cq-num">{r.likes != null ? fmtCount(r.likes) : ''}</td>
                  <td className="cq-num dim">{r.msg_id ?? ''}</td>
                  <td className="dim" title={fmtDate(r.done_at ?? r.added_at)}>
                    {fmtAgo(r.done_at ?? r.added_at)}
                  </td>
                  <td className="cq-detail" title={r.last_error || ''}>
                    {r.attempts ? <span className="cq-tries">×{r.attempts}</span> : null}
                    {r.last_error ? clip(r.last_error, 80) : ''}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <Pager offset={offset} shown={rows.length} total={total} stateFilter={stateFilter} />
    </>
  );
}

function Pager({
  offset,
  shown,
  total,
  stateFilter,
}: {
  offset: number;
  shown: number;
  total: number;
  stateFilter: string;
}) {
  const at = (off: number) =>
    href('capture', { params: { tab: 'queue', state: stateFilter, off: off || '' } });
  return (
    <div className="dtable-more">
      <span className="dim">
        {fmtCount(offset + 1)}–{fmtCount(offset + shown)} of {fmtCount(total)}
        {total ? ` · ${fmtPct(offset + shown, total)}` : ''}
      </span>
      <span className="spacer" />
      {offset > 0 && (
        <a className="btn btn-sm" href={at(Math.max(0, offset - PAGE))}>
          ← previous
        </a>
      )}
      {offset + shown < total && (
        <a className="btn btn-sm" href={at(offset + PAGE)}>
          next →
        </a>
      )}
    </div>
  );
}

/**
 * The two kinds of not-landed, kept apart.
 *
 * `next_try_at` is the column that makes this list actionable: a row with a time
 * in the future is *scheduled*, and nothing needs doing. A row with no time is
 * either in flight or has spent every revival.
 */
function FailureTable({
  failures,
}: {
  failures: FetchState<{ ok: boolean; failures: CaptureFailure[] }>;
}) {
  if (failures.error) {
    return (
      <div className="state-box err">
        <div className="head">Could not read the failures</div>
        <div>{failures.error}</div>
      </div>
    );
  }
  const rows = failures.data?.failures || [];
  if (failures.first && failures.loading) {
    return <div className="skel" style={{ height: 160 }} />;
  }
  if (!rows.length) {
    return (
      <div className="state-box">
        <div className="head">Nothing has failed</div>
        <div>Every row is either captured, queued, or deliberately skipped.</div>
      </div>
    );
  }
  const gone = rows.filter((r) => r.state === 'unavailable').length;
  return (
    <>
      <div className="view-hint cap-fail-hint">
        {fmtCount(rows.length - gone)} will be retried on their own — up to six revivals
        each, four hours apart. {fmtCount(gone)} are gone from Instagram and no amount of
        waiting brings them back; they stay in the ledger as evidence the reel was saved.
      </div>
      <div className="dtable-wrap cap-rows">
        <table className="dtable">
          <thead>
            <tr>
              <th className="cq-state">state</th>
              <th>key</th>
              <th className="cq-num">tries</th>
              <th>last tried</th>
              <th>next try</th>
              <th>error</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const meta = STATES.find((s) => s.key === r.state);
              return (
                <tr key={r.key}>
                  <td className="cq-state">
                    <span className={`cchip ${meta?.cls || 'cs-failed'}`} title={meta?.why}>
                      {meta?.label || r.state}
                    </span>
                  </td>
                  <td>
                    <button className="cq-key cq-key-ext" title={r.url} onClick={() => openUrl(r.url)}>
                      {r.key}
                    </button>
                  </td>
                  <td className="cq-num">{r.attempts ?? 0}</td>
                  <td className="dim" title={fmtDate(r.last_try_at)}>
                    {fmtAgo(r.last_try_at)}
                  </td>
                  <td className="dim">
                    {r.state === 'unavailable' ? '—' : fmtIn(r.next_try_at) || 'due now'}
                  </td>
                  <td className="cq-detail" title={r.last_error || ''}>
                    {r.last_error ? clip(r.last_error, 110) : ''}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function ActivityList({
  activity,
}: {
  activity: FetchState<{ ok: boolean; events: CaptureEvent[] }>;
}) {
  if (activity.error) {
    return (
      <div className="state-box err">
        <div className="head">Could not read the log</div>
        <div>{activity.error}</div>
      </div>
    );
  }
  const rows = activity.data?.events || [];
  if (activity.first && activity.loading) return <div className="skel" style={{ height: 200 }} />;
  if (!rows.length) {
    return (
      <div className="state-box">
        <div className="head">Nothing logged yet</div>
        <div>The ledger records every capture, failure, scan and snapshot here.</div>
      </div>
    );
  }
  return (
    <div className="cap-log">
      {rows.map((e) => (
        <div className={`cap-ev ${EVENT_TONE[e.kind] || ''}`} key={e.id}>
          <span className="cap-ev-at dim" title={fmtDate(e.at)}>
            {fmtAgo(e.at)}
          </span>
          <span className="cap-ev-k">{e.kind}</span>
          {e.key ? <span className="cap-ev-key font-mono">{e.key}</span> : null}
          <span className="cap-ev-t">{e.text}</span>
        </div>
      ))}
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════
   Asset backfill
   ══════════════════════════════════════════════════════════════════════ */

/**
 * The second worker: clip sets for videos captured before clip sets existed.
 *
 * Its own thread and its own state machine, deliberately outside the single
 * task slot — a backfill runs for the better part of an hour, and locking out
 * scans and imports for that long is the wrong trade. It matters because a
 * video with an asset set plays instantly and one without has to be streamed
 * from Telegram.
 */
function BackfillCard({
  data,
  busy,
  ready,
  onStart,
  onStop,
}: {
  data?: BackfillStatus | null;
  busy: string;
  ready: boolean;
  onStart: () => void;
  onStop: () => void;
}) {
  if (!data) return null;
  const c = data.counts || {};
  const live = data.state === 'running' || data.state === 'stopping';
  const have = c.with_assets ?? 0;
  const all = c.videos ?? 0;
  const left = c.without_assets ?? 0;

  return (
    <div className="cap-bf">
      <div className="section-h cap-bf-h">
        <Satellite size={13} className="dim" />
        <h2>Fast-playback assets</h2>
        <span className="count">
          {c.error
            ? c.error
            : `${fmtCount(have)} of ${plural(all, 'video')} ready${
                left ? ` · ${fmtCount(left)} without` : ''
              }`}
        </span>
        <span className="spacer" />
        {live ? (
          <button className="btn btn-sm" disabled={Boolean(busy)} onClick={onStop}>
            <Square size={12} /> Stop
          </button>
        ) : (
          <button
            className="btn btn-sm"
            disabled={Boolean(busy) || !ready || !left}
            title={
              !ready
                ? 'set the bot token and channel id first'
                : left
                  ? 'cut and upload clip sets for the videos that have none'
                  : 'every video already has an asset set'
            }
            onClick={onStart}
          >
            <Play size={12} /> Build
          </button>
        )}
      </div>

      {all > 0 && (
        <div className="meter cap-bf-meter" title={`${fmtPct(have, all)} of the archive plays the fast way`}>
          <div className="meter-fill" style={{ width: `${(have / all) * 100}%` }} />
        </div>
      )}

      <div className="cap-bf-row">
        <span className={`cap-pill cap-pill-${data.state}${live ? ' is-live' : ''}`}>
          {data.state}
        </span>
        <span className="dim">{data.error || data.message}</span>
        {data.current?.key ? (
          <>
            <span className="dim">·</span>
            <span className="font-mono">{data.current.key}</span>
            {data.current.phase ? (
              <span className={`cap-phase ph-${data.current.phase}`}>{data.current.phase}</span>
            ) : null}
            {data.current.of ? (
              <span className="dim">
                {data.current.n} of {data.current.of}
              </span>
            ) : null}
          </>
        ) : null}
        <span className="spacer" />
        {data.started_at ? <span className="dim">{fmtAgo(data.started_at)}</span> : null}
      </div>

      {live || data.done || data.failed ? (
        <div className="cap-bf-stats">
          <span>
            <strong className="font-mono">{fmtCount(data.done)}</strong>{' '}
            <span className="dim">done</span>
          </span>
          <span>
            <strong className="font-mono">{fmtCount(data.clips)}</strong>{' '}
            <span className="dim">clips cut</span>
          </span>
          <span>
            <strong className="font-mono">{fmtCount(data.uploads)}</strong>{' '}
            <span className="dim">uploads</span>
          </span>
          {data.failed ? (
            <span>
              <strong className="font-mono cap-fail-n">{fmtCount(data.failed)}</strong>{' '}
              <span className="dim">failed</span>
            </span>
          ) : null}
          {data.skipped ? (
            <span>
              <strong className="font-mono">{fmtCount(data.skipped)}</strong>{' '}
              <span className="dim">skipped</span>
            </span>
          ) : null}
          {data.total ? <span className="dim">of {fmtCount(data.total)} this pass</span> : null}
        </div>
      ) : null}

      {data.autostart?.armed || data.autostart?.state !== 'off' ? (
        <div className="dim cap-bf-auto">
          autostart: {data.autostart.state}
          {data.autostart.message ? ` — ${data.autostart.message}` : ''}
        </div>
      ) : null}

      {data.notes?.length ? (
        <div className="cap-bf-notes">
          {data.notes.map((note, i) => (
            <div className="cap-bf-note" key={i}>
              {note}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

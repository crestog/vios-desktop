/**
 * views/Admin.tsx — the tab you open when something else is broken.
 *
 * Four concerns, and they are four addresses (`/admin?section=wire`) rather
 * than four cards on one long page: the reason this screen gets opened is that
 * something *else* said "Telegram is not configured", and the link in that
 * message has to land on the credential form itself, not on a page to scroll.
 *
 *   - **Credentials.** Six secrets, rendered from the server's own field list
 *     rather than a copy of it here, so a seventh credential upstream needs no
 *     change in this file. Presence and origin are shown; a value never is —
 *     `creds.describe()` is built so this screen *cannot* leak one.
 *   - **The wire.** Whether this build still reads what the other program
 *     writes. `ahead` is the only verdict that is an alarm, and it says what to
 *     do about it.
 *   - **Restore.** Effects before the click, and a scope sentence that admits
 *     the database it replaces is not the database the reader reads.
 *   - **Sources.** Every bundle and shard ever imported, and every column
 *     reflection put in the text index.
 *
 * Two absences are deliberate. There is **no export button**, for the reason
 * `server/admin_routes.py` gives at length: an export from this laptop would
 * pin a manifest of an empty index over the one the Kaggle side restores from.
 * And there is **no "reclaim space" delete** — nothing in this application
 * evicts, on purpose, and a button that breaks that rule has to name every file
 * it is about to remove before it earns a place here.
 */

import { useEffect, useState } from 'react';
import {
  ArrowDownToLine,
  Check,
  Eye,
  EyeOff,
  FileWarning,
  KeyRound,
  Layers,
  Link2,
  RotateCcw,
  Save,
  ShieldCheck,
  Table2,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { href } from '../lib/router';
import {
  ApiError,
  applyRestore,
  forgetCredentials,
  getBundles,
  getCredentials,
  getRestore,
  getWire,
  inspectRestore,
  saveCredentials,
} from '../lib/api';
import { useFetch, type FetchState } from '../lib/useFetch';
import { clip, fmtAgo, fmtBytes, fmtCount, fmtDate, plural } from '../lib/format';
import type {
  BundleRow,
  BundlesResponse,
  CredentialFields,
  RestorePlan,
  RestoreStatus,
  StoredCredentials,
  WireReport,
} from '../types';

type Section = 'credentials' | 'wire' | 'restore' | 'sources';

const SECTIONS: Array<{
  key: Section;
  label: string;
  Icon: LucideIcon;
}> = [
  { key: 'credentials', label: 'Credentials', Icon: KeyRound },
  { key: 'wire', label: 'The wire', Icon: Link2 },
  { key: 'restore', label: 'Restore', Icon: ArrowDownToLine },
  { key: 'sources', label: 'Sources', Icon: Layers },
];

/** The four without which nothing reaches the channel. */
const TELEGRAM = new Set(['bot_token', 'channel_id', 'api_id', 'api_hash']);

/**
 * How each credential is typed, which is the whole of the difference between a
 * usable form and a hostile one: an id is digits and must stay readable while
 * being checked against Telegram's own page, a token is a secret and should
 * mask, and a cookie jar is four lines of text that a single-line input turns
 * into a horizontal scroll.
 */
const KIND: Record<string, 'secret' | 'id' | 'blob'> = {
  bot_token: 'secret',
  api_hash: 'secret',
  hf_token: 'secret',
  channel_id: 'id',
  api_id: 'id',
  ig_cookies: 'blob',
};

const VERDICT: Record<string, { label: string; cls: string }> = {
  current: { label: 'schema current', cls: 'adm-v-ok' },
  behind: { label: 'reads older files', cls: 'adm-v-info' },
  ahead: { label: 'channel is newer', cls: 'adm-v-bad' },
  empty: { label: 'nothing imported', cls: 'adm-v-idle' },
  unknown: { label: 'cannot check', cls: 'adm-v-warn' },
};

const R_STATE: Record<string, { label: string; cls: string }> = {
  idle: { label: 'idle', cls: 'adm-r-idle' },
  running: { label: 'running', cls: 'adm-r-run' },
  ready: { label: 'plan ready', cls: 'adm-r-ready' },
  done: { label: 'done', cls: 'adm-r-done' },
  error: { label: 'error', cls: 'adm-r-err' },
};

const ORIGIN: Record<string, string> = {
  env: 'environment',
  kaggle: 'Kaggle secret',
  file: 'stored file',
  typed: 'typed here',
};

/** `ApiError.detail` is the server's own sentence; `message` prefixes a code. */
function why(e: unknown): string {
  if (e instanceof ApiError) {
    return e.status === 0 ? 'The local server is not answering.' : e.detail;
  }
  return String((e as Error)?.message || e);
}

export default function AdminView({ route }: ViewProps) {
  const asked = route.params.get('section') || 'credentials';
  const section = (SECTIONS.some((s) => s.key === asked) ? asked : 'credentials') as Section;

  const creds = useFetch((s) => getCredentials(s), []);
  const wire = useFetch((s) => getWire(s), []);
  const restore = useFetch((s) => getRestore(s), []);
  // The only one gated on its tab. The other three feed the rail badges, and a
  // badge that is blank until you click the thing it describes is not a badge.
  const sources = useFetch((s) => getBundles(s), [], { enabled: section === 'sources' });

  // Polled only while a restore thread is alive. A finished job's status does
  // not change again, so a timer that keeps asking is a timer that keeps waking
  // the disk for an answer it already has.
  const running = restore.data?.state === 'running';
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => restore.reload(), 1200);
    return () => window.clearInterval(id);
  }, [running, restore.reload]);

  const fields = creds.data?.fields || [];
  const present = fields.filter((f) => f.present).length;
  const verdict = wire.data ? VERDICT[wire.data.verdict] : null;

  const badge: Record<Section, string> = {
    credentials: creds.data
      ? creds.data.complete
        ? 'ready'
        : `${present}/${fields.length || 6}`
      : '',
    wire: wire.data ? verdict?.label || wire.data.verdict : '',
    restore: restore.error
      ? 'n/a'
      : restore.data
        ? R_STATE[restore.data.state]?.label || restore.data.state
        : '',
    sources: wire.data?.imported.readable
      ? fmtCount(wire.data.imported.bundles + wire.data.imported.shards)
      : '',
  };

  function reloadAll() {
    creds.reload();
    wire.reload();
    restore.reload();
    if (section === 'sources') sources.reload();
  }

  return (
    <div className="view view-split adm-view">
      <div className="view-bar">
        <ShieldCheck size={14} className="dim" />
        <strong>Admin</strong>
        {wire.data && (
          <span className={`adm-pill ${verdict?.cls || 'adm-v-idle'}`}>
            {verdict?.label || wire.data.verdict}
          </span>
        )}
        {creds.data && !creds.data.complete && (
          <span className="adm-pill adm-v-warn">Telegram incomplete</span>
        )}
        <span className="spacer" />
        <button className="btn btn-sm" onClick={reloadAll} title="re-read all four">
          <RotateCcw size={13} /> Refresh
        </button>
      </div>

      <div className="split">
        <aside className="rail rail-left" aria-label="Admin sections">
          <div className="rail-head">Sections</div>
          <div className="rail-body">
            {SECTIONS.map(({ key, label, Icon }) => (
              <a
                key={key}
                className={`tbl adm-tab${key === section ? ' is-active' : ''}`}
                href={href('admin', { params: { section: key } })}
              >
                <span className="tbl-n">
                  <Icon size={12} /> {label}
                </span>
                <span className="tbl-r">{badge[key]}</span>
              </a>
            ))}
          </div>
          <div className="adm-rail-foot">
            <div className="dim">credential file</div>
            <div className="adm-path" title={String(creds.data?.local_file || '')}>
              {creds.data?.local_file_present
                ? creds.data.local_file
                : creds.data
                  ? 'nothing stored yet'
                  : '…'}
            </div>
            {restore.data?.target && (
              <>
                <div className="dim">restore target</div>
                <div className="adm-path" title={restore.data.target}>
                  {restore.data.target}
                </div>
              </>
            )}
          </div>
        </aside>

        <div className="split-main adm-main">
          {section === 'credentials' && <Credentials state={creds} />}
          {section === 'wire' && <Wire state={wire} />}
          {section === 'restore' && <Restore state={restore} />}
          {section === 'sources' && <Sources state={sources} />}
        </div>
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// CREDENTIALS
// ══════════════════════════════════════════════════════════════════════════
/**
 * The form is always empty, and that is the design rather than a shortcut.
 *
 * A field pre-filled with a masked value has to answer "is this the real token
 * or six dots pretending" every time it is edited, and the only honest way to
 * fill it is to send the secret to the browser. So presence is a chip beside the
 * label, the input is blank, and a blank field on submit means *leave that one
 * alone* — which is what lets a user fix one wrong credential out of six without
 * retyping the other five.
 */
function Credentials({ state }: { state: FetchState<StoredCredentials> }) {
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const fields = state.data?.fields || [];
  const filled = Object.entries(typed).filter(([, v]) => v.trim().length > 0);

  async function save() {
    if (busy || !filled.length) return;
    setBusy(true);
    setMsg(null);
    try {
      const body: CredentialFields = {};
      for (const [k, v] of filled) (body as Record<string, string>)[k] = v.trim();
      const res = await saveCredentials(body);
      setTyped({});
      setMsg({
        kind: 'ok',
        text:
          `Saved ${res.changed.join(', ')} to ${res.path}. ` +
          `${plural(res.exported.length, 'variable')} set in this process too, so ` +
          `it is live now — nothing to restart.`,
      });
      state.reload();
    } catch (e) {
      setMsg({ kind: 'err', text: why(e) });
    } finally {
      setBusy(false);
    }
  }

  async function forget() {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await forgetCredentials();
      setArmed(false);
      setMsg({
        kind: 'ok',
        text: res.removed ? res.effect : 'There was no stored credential file to remove.',
      });
      state.reload();
    } catch (e) {
      setMsg({ kind: 'err', text: why(e) });
    } finally {
      setBusy(false);
    }
  }

  if (state.error) {
    return <ErrBox head="Could not read the credential store" msg={state.error} />;
  }
  if (state.first && state.loading) {
    return <div className="skel" style={{ height: 300 }} />;
  }

  const groups: Array<{ title: string; note: string; rows: typeof fields }> = [
    {
      title: 'Telegram',
      note:
        'All four are required before a channel scan, the mirror, or a restore ' +
        'can reach the archive. Three come from my.telegram.org; the channel id ' +
        'is the numeric one, starting -100.',
      rows: fields.filter((f) => TELEGRAM.has(f.name)),
    },
    {
      title: 'Optional',
      note:
        'Model weights and Instagram capture. Search, the graph and playback ' +
        'work with both of these unset.',
      rows: fields.filter((f) => !TELEGRAM.has(f.name)),
    },
  ];
  const fromEnv = fields.filter((f) => f.present && f.source === 'env');

  return (
    <>
      <div className="section-h">
        <KeyRound size={13} className="dim" />
        <h2>Credentials</h2>
        <span className="count">
          {state.data?.complete ? 'Telegram ready' : 'Telegram incomplete'}
          {state.data?.local_file_present ? ' · stored on disk' : ' · nothing stored'}
        </span>
      </div>

      <p className="view-hint adm-note">
        Values go in and never come out. Each row reports whether a secret is set and{' '}
        <em>where it came from</em> — the inputs are blank by design, and a field left
        blank changes nothing.
      </p>

      {groups.map((g) => (
        <div className="adm-group" key={g.title}>
          <div className="adm-group-h">
            <strong>{g.title}</strong>
            <span className="dim">{g.note}</span>
          </div>
          {g.rows.map((f) => (
            <div className="adm-field" key={f.name}>
              <div className="adm-field-h">
                <label htmlFor={`cred-${f.name}`}>{f.label}</label>
                <span
                  className={`adm-src${f.present ? ' is-set' : ''}`}
                  title={
                    f.aliases.length
                      ? `also read from ${f.aliases.join(', ')}`
                      : 'no alternative names'
                  }
                >
                  {f.present ? ORIGIN[f.source] || f.source || 'set' : 'not set'}
                </span>
              </div>
              <div className="adm-field-d">{f.description}</div>
              {KIND[f.name] === 'blob' ? (
                <textarea
                  id={`cred-${f.name}`}
                  className="input-text adm-blob"
                  rows={3}
                  spellCheck={false}
                  autoComplete="off"
                  placeholder={f.present ? 'stored — paste to replace' : 'paste the cookie jar'}
                  value={typed[f.name] || ''}
                  onChange={(e) => setTyped((t) => ({ ...t, [f.name]: e.target.value }))}
                />
              ) : (
                <input
                  id={`cred-${f.name}`}
                  className="input-text adm-input"
                  type={show || KIND[f.name] === 'id' ? 'text' : 'password'}
                  inputMode={KIND[f.name] === 'id' ? 'numeric' : undefined}
                  spellCheck={false}
                  autoComplete="off"
                  placeholder={f.present ? 'stored — type to replace' : 'not set'}
                  value={typed[f.name] || ''}
                  onChange={(e) => setTyped((t) => ({ ...t, [f.name]: e.target.value }))}
                />
              )}
            </div>
          ))}
        </div>
      ))}

      <div className="adm-actions">
        <button className="btn btn-primary" disabled={busy || !filled.length} onClick={save}>
          <Save size={13} />{' '}
          {filled.length
            ? `Save ${filled.length} credential${filled.length > 1 ? 's' : ''}`
            : 'Nothing typed'}
        </button>
        <button className="btn-ghost" onClick={() => setShow((v) => !v)}>
          {show ? <EyeOff size={12} /> : <Eye size={12} />} {show ? 'hide' : 'show'} what I type
        </button>
        <span className="spacer" />
        {armed ? (
          <span className="adm-confirm">
            <span className="dim">Delete {state.data?.local_file}?</span>
            <button className="btn btn-sm btn-danger" disabled={busy} onClick={forget}>
              Yes, forget
            </button>
            <button className="btn btn-sm" disabled={busy} onClick={() => setArmed(false)}>
              Cancel
            </button>
          </span>
        ) : (
          <button
            className="btn btn-sm"
            disabled={busy || !state.data?.local_file_present}
            onClick={() => setArmed(true)}
            title="removes the stored file; this process keeps what it already loaded"
          >
            <Trash2 size={12} /> Forget stored
          </button>
        )}
      </div>

      {msg && (
        <div className={`adm-msg ${msg.kind === 'ok' ? 'is-ok' : 'is-err'}`}>
          {msg.kind === 'ok' ? <Check size={14} /> : <TriangleAlert size={14} />}
          <span>{msg.text}</span>
          <button className="btn-ghost" onClick={() => setMsg(null)}>
            dismiss
          </button>
        </div>
      )}

      {fromEnv.length > 0 && (
        <p className="view-hint adm-note">
          {fromEnv.length === 1 ? 'One credential' : `${fromEnv.length} credentials`} currently
          {fromEnv.length === 1 ? ' comes' : ' come'} from the environment
          ({fromEnv.map((f) => f.label).join(', ')}). Saving here overwrites that for this
          process as well as storing it — the environment is read before the file, so writing
          only the file would report success and change nothing until a restart.
        </p>
      )}

      {state.data?.on_kaggle && (
        <div className="panel adm-kaggle">
          <div className="panel-h">Kaggle secret store</div>
          <div className="dim">{state.data.kaggle_reason || 'the store answered'}</div>
          {(state.data.kaggle_advice || []).map((line, i) => (
            <div className="dim" key={i}>
              · {line}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// THE WIRE
// ══════════════════════════════════════════════════════════════════════════
/**
 * One question: can this build still read what the other program writes?
 *
 * Three numbers answer it and the middle one is the only measurement — the
 * highest schema any file that *actually imported* was written with. `ahead`
 * means the channel has moved on without this laptop, and the honest response
 * is to pull the upstream commit rather than keep scanning and hope the missing
 * columns were ones nothing reads.
 */
function Wire({ state }: { state: FetchState<WireReport> }) {
  if (state.error) return <ErrBox head="Could not read the wire report" msg={state.error} />;
  if (state.first && state.loading) return <div className="skel" style={{ height: 260 }} />;
  const w = state.data;
  if (!w) return null;

  const v = VERDICT[w.verdict] || { label: w.verdict, cls: 'adm-v-idle' };
  const p = w.provenance;
  const im = w.imported;
  const tiers = Object.entries(w.schema.by_schema).sort((a, b) => a[0].localeCompare(b[0]));

  return (
    <>
      <div className="section-h">
        <Link2 size={13} className="dim" />
        <h2>The wire</h2>
        <span className="count">one Telegram channel, two programs, no shared code</span>
      </div>

      <div className={`adm-verdict ${v.cls}`}>
        <div className="adm-verdict-h">
          {w.verdict === 'ahead' ? (
            <TriangleAlert size={15} />
          ) : w.verdict === 'current' ? (
            <Check size={15} />
          ) : (
            <FileWarning size={15} />
          )}
          <strong>{v.label}</strong>
        </div>
        <p>{w.headline}</p>
      </div>

      {w.wire_stale && (
        <div className="adm-warn">
          <TriangleAlert size={14} />
          <div>
            <strong>WIRE.md is stale.</strong> It records schema {p.schema_at_commit} and this
            tree's constant is {w.schema.ours}. That document is the only thing keeping the two
            repositories in step, so it has to be updated with the code — <code>{p.path}</code>.
          </div>
        </div>
      )}

      <div className="stat-row">
        <Stat n={w.schema.ours} l="schema here" />
        <Stat n={w.schema.highest_seen} l="highest imported" />
        <Stat n={w.schema.at_commit} l="wire.md records" />
      </div>

      {tiers.length > 0 && (
        <div className="adm-chips">
          {tiers.map(([k, n]) => (
            <span className="adm-chip" key={k}>
              schema {k} <b>{fmtCount(n)}</b>
            </span>
          ))}
        </div>
      )}

      <div className="panel adm-panel">
        <div className="panel-h">
          <Layers size={13} />
          What has come across
        </div>
        {im.readable ? (
          <>
            <dl className="kv">
              <dt>Bundles</dt>
              <dd>{fmtCount(im.bundles)}</dd>
              <dt>Shards</dt>
              <dd>{fmtCount(im.shards)}</dd>
              <dt>Failed</dt>
              <dd className={im.failed ? 'adm-bad' : undefined}>
                {im.failed ? `${fmtCount(im.failed)} — the next scan retries them` : 'none'}
              </dd>
              <dt>Bytes</dt>
              <dd>{fmtBytes(im.bytes)}</dd>
              <dt>Newest</dt>
              <dd>
                {im.newest_at
                  ? `${fmtDate(im.newest_at)} · ${fmtAgo(im.newest_at)}`
                  : 'nothing imported yet'}
              </dd>
            </dl>
            <p className="view-hint">{w.note}</p>
          </>
        ) : (
          <p className="view-hint">
            There is no bundles table yet, which means the channel has not been scanned on this
            machine. That is the expected state before the first boot finishes — the scan writes it.
          </p>
        )}
      </div>

      <div className="panel adm-panel">
        <div className="panel-h">Provenance</div>
        <dl className="kv">
          <dt>Upstream</dt>
          <dd>{p.upstream || 'not recorded'}</dd>
          <dt>Commit</dt>
          <dd className="adm-mono">{p.commit ? p.commit.slice(0, 12) : 'not recorded'}</dd>
          <dt>Lifted</dt>
          <dd>{p.lifted_on || 'not recorded'}</dd>
          <dt>Document</dt>
          <dd className="adm-mono">{p.path}</dd>
        </dl>
        {!p.parsed && (
          <p className="view-hint">
            {p.note ||
              'WIRE.md is present but its provenance table did not parse, so the numbers above ' +
                'could not be read from it. Rewording that table is enough to cause this.'}
          </p>
        )}
      </div>
    </>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// RESTORE
// ══════════════════════════════════════════════════════════════════════════
/**
 * Pull the newest pinned bundle out of the channel and load it.
 *
 * Two things make this safe to put behind a button. `inspect` performs the whole
 * download and reports what *would* change while writing nothing, and the apply
 * that follows is pinned to `plan.seq` — the bundle the plan on screen describes,
 * not whatever the channel happens to hold by the time the button is pressed.
 *
 * One thing makes it easy to misread, so `scope` is rendered above every
 * control: what this replaces is `config.DB_PATH`, the other plane's database.
 * The reader's own `atlas.db` is built by the channel scan and is not touched, so
 * a restore here does not repopulate this window. That sentence comes from the
 * server rather than being written into the button, because the route is the
 * thing that knows which file it is about to overwrite.
 */
function Restore({ state }: { state: FetchState<RestoreStatus> }) {
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [err, setErr] = useState('');

  if (state.error) return <ErrBox head="Could not read the restore state" msg={state.error} />;
  if (state.first && state.loading) return <div className="skel" style={{ height: 300 }} />;
  const st = state.data;
  if (!st) return null;

  const running = st.state === 'running';
  const s = R_STATE[st.state] || { label: st.state, cls: 'adm-r-idle' };
  const plan = st.plan;
  const blocked = st.missing.length > 0;

  async function go(kind: 'inspect' | 'apply') {
    setBusy(true);
    setErr('');
    setArmed(false);
    try {
      if (kind === 'inspect') await inspectRestore();
      else await applyRestore(plan?.seq ?? null);
      state.reload();
    } catch (e) {
      setErr(why(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="section-h">
        <RotateCcw size={13} className="dim" />
        <h2>Restore</h2>
        <span className={`adm-pill ${s.cls}`}>{s.label}</span>
        {st.mode && <span className="count">{st.mode}</span>}
      </div>

      <p className="view-hint adm-scope">{st.scope}</p>
      <dl className="kv">
        <dt>Target</dt>
        <dd className="adm-mono">{st.target}</dd>
      </dl>

      {blocked && (
        <div className="adm-warn">
          <TriangleAlert size={14} />
          <div>
            <strong>Telegram is not configured.</strong> The bundle lives in the channel, so
            nothing can be fetched until {st.missing.join(', ')}{' '}
            {st.missing.length === 1 ? 'is' : 'are'} set.{' '}
            <a href={href('admin', { params: { section: 'credentials' } })}>
              Enter them in Credentials
            </a>
            .
          </div>
        </div>
      )}

      <div className="adm-actions">
        <button className="btn" disabled={busy || running || blocked} onClick={() => go('inspect')}>
          <Layers size={14} />
          {running ? 'Working…' : 'Inspect the newest bundle'}
        </button>

        {plan &&
          !running &&
          (armed ? (
            <>
              <button className="btn btn-danger" disabled={busy} onClick={() => go('apply')}>
                <ArrowDownToLine size={14} />
                Replace {st.target.split(/[\\/]/).pop()} now
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => setArmed(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button className="btn btn-danger" disabled={busy} onClick={() => setArmed(true)}>
              <ArrowDownToLine size={14} />
              Apply this plan
            </button>
          ))}
      </div>

      {err && <div className="adm-msg is-err">{err}</div>}

      {running && (
        <div className="panel adm-panel">
          <div className="panel-h">
            <span className="spin" />
            {st.stage || 'working'}
          </div>
          <div className="meter">
            <div
              className="meter-fill"
              style={{ width: `${Math.max(2, Math.min(100, st.pct))}%` }}
            />
          </div>
          <div className="adm-prog">
            <b>{st.pct}%</b>
            <span className="dim">{clip(st.detail, 160)}</span>
          </div>
          {st.stalled_s > 45 && (
            <p className="view-hint adm-bad">
              Nothing has transferred for {Math.round(st.stalled_s)}s. A part download this quiet is
              usually a FloodWait being served — the transport sleeps and retries rather than
              failing, so this can sit still for minutes and still finish.
            </p>
          )}
        </div>
      )}

      {st.state === 'error' && st.error && <ErrBox head="The last run failed" msg={st.error} />}

      {plan && <PlanPanel plan={plan} done={st.state === 'done'} />}

      {st.log.length > 0 && (
        <details className="adm-log">
          <summary>Log · {plural(st.log.length, 'line')}</summary>
          <pre>{st.log.slice(-40).join('\n')}</pre>
        </details>
      )}
    </>
  );
}

/**
 * The plan, and after an apply the outcome beside it.
 *
 * The counts table is the whole decision. `destructive` is the server's own
 * verdict but `posts_delta` is the number behind it, and a negative delta is the
 * only genuinely dangerous case: the bundle holds fewer posts than the database
 * it would replace, so applying it loses rows that exist nowhere else. Both are
 * shown, because "this is destructive" without the number is a dialog people
 * learn to click through.
 */
function PlanPanel({ plan, done }: { plan: RestorePlan; done: boolean }) {
  const tables = Array.from(
    new Set([...Object.keys(plan.bundle_counts), ...Object.keys(plan.local_counts)]),
  ).sort();
  const parts = plan.files.reduce((n, f) => n + (f.parts || 0), 0);

  return (
    <>
      <div className="panel adm-panel">
        <div className="panel-h">
          <Layers size={13} />
          The bundle
        </div>
        <dl className="kv">
          <dt>Sequence</dt>
          <dd className="adm-mono">{plan.seq || 'unnamed'}</dd>
          <dt>Created</dt>
          <dd>{plan.created_at || 'not recorded'}</dd>
          <dt>Written by</dt>
          <dd className="adm-mono">
            {plan.code_commit ? plan.code_commit.slice(0, 12) : 'not recorded'}
          </dd>
          <dt>Schema</dt>
          <dd>{plan.schema ?? 'not declared'}</dd>
          <dt>Files</dt>
          <dd>
            {plural(plan.files.length, 'file')}, {plural(parts, 'part')}
          </dd>
          <dt>Download</dt>
          <dd>{plan.download_mb ? `${plan.download_mb.toFixed(1)} MB` : 'already local'}</dd>
        </dl>
      </div>

      {plan.destructive && !done && (
        <div className="adm-warn">
          <TriangleAlert size={14} />
          <div>
            <strong>This would lose rows.</strong>{' '}
            {plan.posts_delta !== null && plan.posts_delta < 0
              ? `The bundle holds ${fmtCount(Math.abs(plan.posts_delta))} fewer posts than the
                 database it replaces, and those rows exist nowhere else once it is overwritten.`
              : `The apply replaces stores that hold more than the bundle does.`}{' '}
            A snapshot of the current file is taken first, and its path is reported below once the
            apply finishes.
          </div>
        </div>
      )}

      {tables.length > 0 && (
        <div className="dtable-wrap">
          <table className="dtable">
            <thead>
              <tr>
                <th>Table</th>
                <th>In the bundle</th>
                <th>Here now</th>
                <th>Change</th>
              </tr>
            </thead>
            <tbody>
              {tables.map((t) => {
                const b = plan.bundle_counts[t] ?? 0;
                const l = plan.local_counts[t] ?? 0;
                const d = b - l;
                return (
                  <tr key={t}>
                    <td className="adm-mono">{t}</td>
                    <td>{fmtCount(b)}</td>
                    <td>{fmtCount(l)}</td>
                    <td className={d < 0 ? 'adm-bad' : d > 0 ? 'adm-ok' : 'dim'}>
                      {d === 0 ? '—' : `${d > 0 ? '+' : '−'}${fmtCount(Math.abs(d))}`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {plan.effects.length > 0 && (
        <div className="panel adm-panel">
          <div className="panel-h">What an apply touches</div>
          <div className="row-list">
            {plan.effects.map((e) => (
              <div className="row-item adm-effect" key={e.target}>
                <span
                  className={`adm-eff ${e.action === 'replaced' ? 'is-repl' : 'is-keep'}`}
                  title={e.impact === 'yes' ? 'changes what this window shows' : ''}
                >
                  {e.action}
                </span>
                <div className="adm-eff-body">
                  <b>{e.target}</b>
                  <div className="dim">{e.detail}</div>
                </div>
              </div>
            ))}
          </div>
          {plan.has_postgres && (
            <p className="view-hint">
              This bundle carries a Postgres dump as well. It is loaded only if <code>psql</code> is
              on PATH — otherwise the run reports that half skipped and the rest of the restore
              still lands.
            </p>
          )}
        </div>
      )}

      {plan.outcome && (
        <div className="panel adm-panel adm-outcome">
          <div className="panel-h">
            <Check size={13} />
            Afterwards — measured, not forecast
          </div>
          <dl className="kv">
            <dt>Loaded</dt>
            <dd>{plan.outcome.loaded.length ? plan.outcome.loaded.join(', ') : 'nothing'}</dd>
            <dt>Row counts</dt>
            <dd className={plan.outcome.matches_bundle === false ? 'adm-bad' : undefined}>
              {plan.outcome.matches_bundle === null
                ? 'not comparable'
                : plan.outcome.matches_bundle
                  ? 'match the bundle'
                  : 'do not match the bundle'}
            </dd>
            <dt>Snapshot</dt>
            <dd className="adm-mono">{plan.outcome.snapshot || 'none taken'}</dd>
          </dl>
          {plan.outcome.matches_bundle === false && (
            <div className="adm-chips">
              {Object.entries(plan.outcome.counts_after).map(([t, n]) => (
                <span className="adm-chip" key={t}>
                  {t} <b>{fmtCount(n)}</b>
                </span>
              ))}
            </div>
          )}
          {plan.outcome.next_steps.length > 0 && (
            <ol className="adm-steps">
              {plan.outcome.next_steps.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ol>
          )}
        </div>
      )}
    </>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// SOURCES
// ══════════════════════════════════════════════════════════════════════════
/**
 * Everything this database was built out of, and every column the text index
 * reads.
 *
 * One table holds both kinds of import, told apart by the `seq` prefix —
 * `import_shard` writes `"shard:…"` into the same eleven columns a manifest
 * import uses. Worth separating rather than hiding behind one count, because the
 * two arrive by completely different paths: a bundle is a pinned snapshot
 * somebody exported, a shard is evidence a session appended as it worked.
 *
 * `failed` rows are shown on purpose and are not an error state. A manifest that
 * would not parse is usually a torn download and the next scan is exactly the
 * retry it needs, so a row here failed once, not forever.
 */
function Sources({ state }: { state: FetchState<BundlesResponse> }) {
  const [kind, setKind] = useState<'all' | 'bundle' | 'shard' | 'failed'>('all');
  const [all, setAll] = useState(false);

  if (state.error) return <ErrBox head="Could not read the imported files" msg={state.error} />;
  if (state.first && state.loading) return <div className="skel" style={{ height: 320 }} />;
  const d = state.data;
  if (!d) return null;

  const isShard = (r: BundleRow) => (r.seq || '').startsWith('shard:');
  const rows = d.bundles.filter((r) =>
    kind === 'all'
      ? true
      : kind === 'failed'
        ? r.status !== 'ok'
        : kind === 'shard'
          ? isShard(r)
          : !isShard(r),
  );
  const shown = all ? rows : rows.slice(0, 120);
  const bundles = d.bundles.filter((r) => !isShard(r)).length;
  const failed = d.bundles.filter((r) => r.status !== 'ok').length;

  const FILTERS: Array<{ key: typeof kind; label: string }> = [
    { key: 'all', label: `All ${d.bundles.length}` },
    { key: 'bundle', label: `Bundles ${bundles}` },
    { key: 'shard', label: `Shards ${d.bundles.length - bundles}` },
    { key: 'failed', label: `Failed ${failed}` },
  ];

  return (
    <>
      <div className="section-h">
        <Table2 size={13} className="dim" />
        <h2>Sources</h2>
        <span className="count">{plural(d.bundles.length, 'imported file')}</span>
        <span className="spacer" />
        <div className="segmented">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={f.key === kind ? 'on' : ''}
              onClick={() => {
                setKind(f.key);
                setAll(false);
              }}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {d.bundles.length === 0 ? (
        <p className="view-hint">
          Nothing has been imported yet. The channel scan writes this table, and on a first launch it
          is still walking — Home reports where it is.
        </p>
      ) : (
        <div className="dtable-wrap">
          <table className="dtable">
            <thead>
              <tr>
                <th>Sequence</th>
                <th>Schema</th>
                <th>Written</th>
                <th>Parts</th>
                <th>Size</th>
                <th>Imported</th>
                <th>Rows</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.seq} className={r.status !== 'ok' ? 'adm-failed' : undefined}>
                  <td className="adm-mono" title={r.note || ''}>
                    {isShard(r) && <span className="adm-chip adm-chip-sh">shard</span>}
                    {clip(r.seq.replace(/^shard:/, ''), 42)}
                  </td>
                  <td>{r.schema ?? '—'}</td>
                  <td>{r.created_at || '—'}</td>
                  <td>{r.parts ?? '—'}</td>
                  <td>{r.bytes ? fmtBytes(r.bytes) : '—'}</td>
                  <td title={r.imported_at ? fmtDate(r.imported_at) : ''}>
                    {r.imported_at ? fmtAgo(r.imported_at) : '—'}
                  </td>
                  <td className="dim">
                    {fmtCount(Object.values(r.counts || {}).reduce((n, v) => n + (v || 0), 0))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rows.length > shown.length && (
        <button className="btn btn-ghost btn-sm" onClick={() => setAll(true)}>
          Show the remaining {fmtCount(rows.length - shown.length)}
        </button>
      )}

      <div className="section-h adm-sec2">
        <Layers size={13} className="dim" />
        <h2>The text index</h2>
        <span className="count">
          {plural(d.sources.length, 'column')} reflection chose
        </span>
      </div>
      <p className="view-hint">
        Reflection reads the schema and decides what search reads, so a table added upstream becomes
        searchable here with no code change — and a derived table can volunteer itself by accident.
        Two did, and are now excluded: a UMAP projection's channel label and a scan verdict, neither
        of which is prose. If a row below is not prose either, that is the same failure again.
      </p>
      {d.sources.length === 0 ? (
        <p className="view-hint">
          Reflection has not run yet, or it found nothing to index. Either way search will be empty
          until the boot's index pass finishes.
        </p>
      ) : (
        <div className="dtable-wrap">
          <table className="dtable">
            <thead>
              <tr>
                <th>Table</th>
                <th>Text column</th>
                <th>Row key</th>
                <th>Labelled by</th>
                <th>Moment start</th>
                <th>Reached via</th>
              </tr>
            </thead>
            <tbody>
              {d.sources.map((src, i) => (
                <tr key={`${src.table}.${src.text}.${i}`}>
                  <td className="adm-mono">{src.table}</td>
                  <td className="adm-mono">{src.text}</td>
                  <td className="adm-mono dim">{src.key}</td>
                  <td className="dim">{src.source || '—'}</td>
                  <td className="dim">{src.start || 'untimed'}</td>
                  <td className="dim">{src.via || 'directly'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// TWO SHARED PIECES
// ══════════════════════════════════════════════════════════════════════════
/**
 * Every section fails the same way — a route did not answer — so it says so the
 * same way. `.state-box.err` is the vocabulary the rest of the app already uses
 * for this, and the message is the server's own sentence rather than a status
 * code, because on this machine the server is a thread in the same process and
 * "500" tells the one person who can fix it nothing.
 */
function ErrBox({ head, msg }: { head: string; msg: string }) {
  return (
    <div className="state-box err">
      <div className="head">
        <TriangleAlert size={14} />
        {head}
      </div>
      <p>{msg}</p>
    </div>
  );
}

/** One number with its label. `null` prints an em dash, never `null`. */
function Stat({ n, l }: { n: number | string | null | undefined; l: string }) {
  return (
    <div className="stat">
      <div className="n">{n === null || n === undefined ? '—' : n}</div>
      <div className="l">{l}</div>
    </div>
  );
}




/**
 * components/DrillDown.tsx — no number without a provenance.
 *
 * Every cell in every table, every chip on every card and every edge in the
 * graph can open this panel, and it answers the same four questions each time:
 * *what is this value, where did it come from, who else says it, and what reads
 * it.* It is the concrete form of the rule that this product does not show a
 * number it cannot account for.
 *
 * What it deliberately does **not** show is as important. v1's version printed
 * an `observer` name, a `corroborations` count and a hardcoded
 * `'Qwen3-VL-8B-AWQ'` — none of which `/api/cell` returns. That is the exact
 * failure this panel exists to prevent: a confident-looking attribution that no
 * row in the database supports. So this renders `/api/cell`'s real fields, and
 * where the pass that wrote the row recorded a model or a confidence, it shows
 * up in *the row itself* below, because that is where it actually lives.
 */

import { Fragment, useEffect, useState } from 'react';
import { Copy, ExternalLink, X } from 'lucide-react';
import type { CellProvenance } from '../types';
import { getCell } from '../lib/api';
import { channelOf, chipClass } from '../lib/channels';
import { fmtCount, fmtDur } from '../lib/format';
import { go, href } from '../lib/router';
import { store, type DrillTarget } from '../lib/store';
import { useFetch } from '../lib/useFetch';

/** What each `role` means in one line — the schema's own vocabulary. */
const ROLE_NOTE: Record<string, string> = {
  key: 'the video this row is about',
  start: 'when this claim starts, in seconds',
  end: 'when this claim ends, in seconds',
  content: 'text the search index reads',
  field: 'a plain column — not read by search',
};

function sqlLiteral(v: unknown): string {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return `'${String(v).replace(/'/g, "''")}'`;
}

function sqlFor(c: CellProvenance, target: DrillTarget): string {
  const t = `"${c.table.replace(/"/g, '""')}"`;
  const col = `"${c.column.replace(/"/g, '""')}"`;
  return target.rowid !== undefined
    ? `SELECT ${col} FROM ${t} WHERE rowid = ${target.rowid};`
    : `SELECT * FROM ${t} WHERE ${col} = ${sqlLiteral(c.value)} LIMIT 50;`;
}

function show(v: unknown): string {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export default function DrillDown({ target }: { target: DrillTarget }) {
  const [copied, setCopied] = useState<string | null>(null);
  const { data, error, first, loading } = useFetch(
    (signal) => getCell(target, signal),
    [target.table, target.column, target.rowid, target.value]
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') store.closeDrill();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      window.setTimeout(() => setCopied(null), 1400);
    } catch {
      setCopied('clipboard blocked');
      window.setTimeout(() => setCopied(null), 1400);
    }
  };

  const c = data;
  const timeAt =
    c && c.time_column && c.row ? (c.row[c.time_column] as number | null | undefined) : undefined;

  return (
    <>
      <div className="scrim" onClick={() => store.closeDrill()} />
      <aside className="drawer" role="dialog" aria-label="Where this value came from">
        <header className="drawer-head">
          <div>
            <div className="drawer-eyebrow">where this came from</div>
            <h2 className="drawer-title">
              {target.table}
              <span className="dim">.</span>
              {target.column}
            </h2>
          </div>
          <button className="btn-icon" onClick={() => store.closeDrill()} aria-label="Close">
            <X size={16} />
          </button>
        </header>

        <div className="drawer-body">
          {first && loading && <div className="skel" style={{ height: 120 }} />}
          {error && (
            <div className="state-box err">
              <div className="head">Could not read the provenance</div>
              <div>{error}</div>
            </div>
          )}
          {c && c.ok === false && (
            <div className="state-box">
              <div className="head">No provenance for that cell</div>
              <div>{c.note || 'the server did not recognise that table or column'}</div>
            </div>
          )}

          {c && c.ok !== false && (
            <>
              <section className="panel">
                <div className="panel-h">the value</div>
                <div className="cell-value">
                  {c.value === null || c.value === undefined ? (
                    <span className="dim">NULL — nothing was recorded here</span>
                  ) : (
                    show(c.value)
                  )}
                </div>
                <div className="cell-tags">
                  <span className="tag" title={ROLE_NOTE[c.role] || 'a column of this table'}>
                    {c.role}
                  </span>
                  <span className="tag" title="the declared sqlite type">
                    {c.type || 'untyped'}
                  </span>
                  {c.pk > 0 && <span className="tag">primary key</span>}
                  <span
                    className={`tag${c.indexed ? ' tag-on' : ''}`}
                    title={
                      c.indexed
                        ? 'this table feeds the full-text search index'
                        : 'this table is not searched — it is read when you open a row'
                    }
                  >
                    {c.indexed ? 'searched' : 'not searched'}
                  </span>
                  {c.source && (
                    <span className={chipClass(c.source)} title="the observer this text comes from">
                      {channelOf(c.source)}
                    </span>
                  )}
                </div>
                {ROLE_NOTE[c.role] && <p className="cell-note">{ROLE_NOTE[c.role]}</p>}
              </section>

              {(c.video_key || timeAt !== undefined) && (
                <section className="panel">
                  <div className="panel-h">the reel</div>
                  <div className="drawer-actions">
                    {c.video_key && (
                      <a
                        className="btn btn-sm"
                        href={href('watch', {
                          key: c.video_key,
                          params:
                            typeof timeAt === 'number' ? { t: timeAt.toFixed(2) } : undefined,
                        })}
                      >
                        <ExternalLink size={12} />
                        {typeof timeAt === 'number'
                          ? `open at ${timeAt.toFixed(2)}s`
                          : 'open the player'}
                      </a>
                    )}
                    {c.video?.title && <span className="dim">{c.video.title}</span>}
                    {c.video?.duration !== undefined && (
                      <span className="dim">{fmtDur(c.video.duration)}</span>
                    )}
                  </div>
                </section>
              )}

              <section className="panel">
                <div className="panel-h">
                  the whole row
                  <span className="dim">
                    {' '}
                    — every column, as stored. Click one to follow it.
                  </span>
                </div>
                <dl className="kv">
                  {Object.entries(c.row || {}).map(([k, v]) => (
                    // A Fragment rather than a wrapping div: `.kv` is a two-column
                    // grid and dt/dd have to be its direct children, or every row
                    // sizes its own columns and the labels stop lining up.
                    <Fragment key={k}>
                      <dt>{k}</dt>
                      <dd>
                        <button
                          className="kv-link"
                          title={`provenance of ${c.table}.${k}`}
                          onClick={() =>
                            store.openDrill({
                              table: c.table,
                              column: k,
                              rowid: target.rowid,
                              value: target.rowid === undefined ? show(v) : undefined,
                            })
                          }
                        >
                          {v === null || v === undefined ? (
                            <span className="dim">NULL</span>
                          ) : (
                            show(v)
                          )}
                        </button>
                      </dd>
                    </Fragment>
                  ))}
                </dl>
              </section>

              {c.refers_to && (
                <section className="panel">
                  <div className="panel-h">
                    it points at
                    <span className="dim">
                      {' '}
                      — {c.refers_to.table} on {c.refers_to.on}
                    </span>
                  </div>
                  <dl className="kv">
                    {Object.entries(c.refers_to.row || {}).map(([k, v]) => (
                      <Fragment key={k}>
                        <dt>{k}</dt>
                        <dd>{show(v)}</dd>
                      </Fragment>
                    ))}
                  </dl>
                </section>
              )}

              {(c.same_value !== null || (c.elsewhere && c.elsewhere.length > 0)) && (
                <section className="panel">
                  <div className="panel-h">who else says it</div>
                  {c.same_value !== null && c.same_value !== undefined && (
                    <p className="cell-note">
                      {fmtCount(c.same_value)} {c.same_value === 1 ? 'row' : 'rows'} in{' '}
                      <code>{c.table}</code>{' '}
                      {c.same_value === 1 ? 'carries' : 'carry'} this same value.
                    </p>
                  )}
                  {c.elsewhere && c.elsewhere.length > 0 && (
                    <ul className="else-list">
                      {c.elsewhere.map((e) => (
                        <li key={`${e.table}.${e.column}`}>
                          <button
                            className="kv-link"
                            onClick={() => {
                              store.closeDrill();
                              go('data', { params: { table: e.table, q: show(c.value) } });
                            }}
                          >
                            {e.table}.{e.column}
                          </button>
                          <span className="dim">
                            {' '}
                            {fmtCount(e.rows)} {e.rows === 1 ? 'row' : 'rows'}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              )}

              <section className="panel">
                <div className="panel-h">the query that returns it</div>
                <pre className="sql">{sqlFor(c, target)}</pre>
                <div className="drawer-actions">
                  <button
                    className="btn btn-sm"
                    onClick={() => copy(sqlFor(c, target), 'sql')}
                  >
                    <Copy size={12} /> {copied === 'sql' ? 'copied' : 'copy SQL'}
                  </button>
                  <button
                    className="btn btn-sm"
                    onClick={() => {
                      store.closeDrill();
                      go('data', { params: { table: c.table } });
                    }}
                  >
                    open {c.table}
                  </button>
                </div>
              </section>
            </>
          )}
        </div>
      </aside>
    </>
  );
}

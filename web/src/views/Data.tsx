/**
 * views/Data.tsx — the database, with nothing hidden and nothing decorated.
 *
 * The premise of this screen is that a claim you cannot check is worth less than
 * one you can. Every table Atlas writes is browsable here, every cell is a link
 * into the provenance panel, and the request that produced what you are looking
 * at is printed at the bottom so you can run it yourself.
 *
 * Two decisions worth writing down:
 *
 *   - **Paging here walks `offset`, unlike Search.** That looks inconsistent
 *     until you see why Search cannot: a ranked FTS query has no stable order
 *     between calls, so an offset walk duplicates and drops rows. `/api/table`
 *     orders by an explicit column — or by rowid when you pick none — which is
 *     stable, so offset is both correct and the only way to reach row 40,000
 *     without holding the first 39,999 in memory.
 *
 *   - **No `/api/query` box.** A free-text SQL console against the live database
 *     is one `DROP TABLE` from an archive you cannot rebuild without re-reading
 *     the whole channel. The row filter and the sort cover what a console is
 *     usually for; the drill panel covers the rest by showing the SQL *it* ran.
 *
 * `getTable` returns `columns`, `types`, `rows`, `rowids` — the wire format
 * describes itself, so this view does not need to know a single column name in
 * advance. That is what lets a new Atlas table appear here with no UI change.
 */

import { useEffect, useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Copy, Play, Table2, X } from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { go, href, num } from '../lib/router';
import { getSchema, getTable } from '../lib/api';
import { store } from '../lib/store';
import { useDebounced, useFetch } from '../lib/useFetch';
import { clip, fmtCount, fmtDur, fmtT } from '../lib/format';
import type { SchemaColumn, SchemaTable } from '../types';

const ROWS = 50;

/** What a role means, in the words of someone who has not read the schema. */
const ROLE_NOTE: Record<SchemaColumn['role'], string> = {
  key: 'identifies the row — usually the content hash of a video',
  start: 'when this evidence starts, in seconds into the reel',
  end: 'when it stops',
  content: 'the text itself — this is what search reads',
  field: 'a plain attribute',
};

export default function DataView({ route }: ViewProps) {
  const p = route.params;
  const table = p.get('table') || '';
  const urlQ = p.get('q') || '';
  const order = p.get('order') || '';
  const desc = p.get('desc') === '1';
  const page = Math.max(0, num(p, 'page') ?? 0);

  const [filter, setFilter] = useState(urlQ);
  useEffect(() => setFilter(urlQ), [urlQ]);
  const typed = useDebounced(filter, 200);

  useEffect(() => {
    if (typed === urlQ) return;
    go('data', {
      params: { table, q: typed, order, desc: desc ? '1' : '', page: '' },
      replace: true,
    });
  }, [typed]);

  // Not `useFetch(getSchema, …)`: `getSchema(samples, signal)` takes the signal
  // *second*, so passing it bare would hand the AbortSignal to `samples` and
  // ask the server for `?samples=[object AbortSignal]`.
  const schema = useFetch((signal) => getSchema(0, signal), []);
  const rows = useFetch(
    (signal) =>
      getTable(
        table,
        { limit: ROWS, offset: page * ROWS, q: urlQ, order, desc },
        signal
      ),
    [table, page, urlQ, order, desc],
    { enabled: table.length > 0 }
  );

  const tables = useMemo(() => {
    const list = schema.data?.tables || [];
    return [...list].sort((a, b) => b.rows - a.rows);
  }, [schema.data]);

  const meta: SchemaTable | undefined = useMemo(
    () => tables.find((t) => t.name === table),
    [tables, table]
  );
  const roleOf = useMemo(() => {
    const m = new Map<string, SchemaColumn>();
    for (const c of meta?.columns || []) m.set(c.name, c);
    return m;
  }, [meta]);

  const res = rows.data;
  const total = res?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / ROWS));
  const keyCol = res?.columns.findIndex((c) => c === 'video_key') ?? -1;

  const move = (patch: Record<string, string>) =>
    go('data', {
      params: { table, q: urlQ, order, desc: desc ? '1' : '', page: String(page), ...patch },
    });

  const sortBy = (col: string) => {
    if (order === col) move({ desc: desc ? '' : '1', page: '' });
    else move({ order: col, desc: '', page: '' });
  };

  // The literal endpoint, printed so the table can be verified outside the app.
  // `/api` is `api.ts`'s private constant on purpose — one place decides where
  // the server lives — so this rebuilds the path rather than importing it.
  const url = table
    ? `/api/table/${encodeURIComponent(table)}?limit=${ROWS}&offset=${page * ROWS}${
        urlQ ? `&q=${encodeURIComponent(urlQ)}` : ''
      }${order ? `&order=${encodeURIComponent(order)}&desc=${desc ? 'true' : 'false'}` : ''}`
    : '';

  return (
    <div className="view view-split">
      <div className="view-bar">
        <Table2 size={14} className="dim" />
        <strong>{table || 'The raw database'}</strong>
        {meta && (
          <span className="dim">
            {fmtCount(meta.rows)} rows · {meta.columns.length} columns
            {meta.indexed ? ' · full-text indexed' : ''}
          </span>
        )}
        <span className="spacer" />
        {table && (
          <>
            <div className="search-box">
              <input
                className="input-text"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={`filter ${table}…`}
                spellCheck={false}
                aria-label={`Filter rows of ${table}`}
              />
              {filter && (
                <button
                  className="btn-icon"
                  onClick={() => setFilter('')}
                  title="clear the filter"
                >
                  <X size={12} />
                </button>
              )}
            </div>
            <span className="dim">
              {res
                ? `${fmtCount(res.offset + 1)}–${fmtCount(
                    Math.min(res.offset + res.rows.length, total)
                  )} of ${fmtCount(total)}`
                : ''}
            </span>
            <button
              className="btn-icon"
              disabled={page <= 0}
              onClick={() => move({ page: String(page - 1) })}
              title="previous page"
            >
              <ChevronLeft size={14} />
            </button>
            <button
              className="btn-icon"
              disabled={page + 1 >= pages}
              onClick={() => move({ page: String(page + 1) })}
              title="next page"
            >
              <ChevronRight size={14} />
            </button>
          </>
        )}
      </div>

      <div className="split">
        <aside className="rail rail-left" aria-label="Tables">
          <div className="rail-head">
            Tables
            {schema.data?.fingerprint && (
              <span className="dim" title="the schema fingerprint — it changes when Atlas adds or renames a column">
                {String(schema.data.fingerprint).slice(0, 8)}
              </span>
            )}
          </div>
          <div className="rail-body">
            {schema.first && schema.loading ? (
              Array.from({ length: 10 }, (_, i) => (
                <div className="skel" style={{ height: 26, margin: '4px 0' }} key={i} />
              ))
            ) : schema.error ? (
              <div className="state-box err">
                <div className="head">Could not read the schema</div>
                <div>{schema.error}</div>
              </div>
            ) : (
              tables.map((t) => (
                <a
                  key={t.name}
                  className={`tbl${t.name === table ? ' is-active' : ''}`}
                  href={href('data', { params: { table: t.name } })}
                  title={`${t.name} — ${fmtCount(t.rows)} rows${
                    t.key ? `, keyed on ${t.key}` : ', no key column'
                  }`}
                >
                  <span className="tbl-n">{t.name}</span>
                  <span className="tbl-r">{fmtCount(t.rows)}</span>
                </a>
              ))
            )}
          </div>
        </aside>

        <div className="split-main">
          {!table ? (
            <SchemaOverview tables={tables} loading={schema.first && schema.loading} />
          ) : rows.error ? (
            <div className="state-box err">
              <div className="head">Could not read {table}</div>
              <div>{rows.error}</div>
            </div>
          ) : rows.first && rows.loading ? (
            <div className="dtable-wrap">
              {Array.from({ length: 14 }, (_, i) => (
                <div className="skel" style={{ height: 24, margin: '3px 0' }} key={i} />
              ))}
            </div>
          ) : !res || !res.rows.length ? (
            <div className="state-box">
              <div className="head">No rows{urlQ ? ' match that filter' : ''}</div>
              <div>
                {res?.note ||
                  (urlQ
                    ? `Nothing in ${table} contains “${urlQ}”. The filter is a substring match across the text columns, not a full-text query.`
                    : `${table} exists but is empty.`)}
              </div>
            </div>
          ) : (
            <>
              {res.note && <div className="view-hint">{res.note}</div>}
              <div className="dtable-wrap">
                <table className="dtable">
                  <thead>
                    <tr>
                      <th className="dt-rowid" title="the sqlite rowid — the drill panel uses it to find this exact row again">
                        #
                      </th>
                      {keyCol >= 0 && <th className="dt-open" title="open the reel this row is about" />}
                      {res.columns.map((c, ci) => {
                        const col = roleOf.get(c);
                        return (
                          <th key={c} className={col ? `dt-role-${col.role}` : undefined}>
                            <button
                              className="dt-h"
                              onClick={() => sortBy(c)}
                              title={`${c} · ${res.types[ci] || 'unknown type'}${
                                col ? ` · ${ROLE_NOTE[col.role]}` : ''
                              }${col?.source ? ` · written by ${col.source}` : ''} — click to sort`}
                            >
                              {c}
                              {order === c &&
                                (desc ? <ArrowDown size={10} /> : <ArrowUp size={10} />)}
                            </button>
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {res.rows.map((row, ri) => {
                      const rowid = res.rowids?.[ri];
                      const vkey = keyCol >= 0 ? String(row[keyCol] ?? '') : '';
                      return (
                        <tr key={rowid ?? ri}>
                          <td className="dt-rowid">{rowid ?? '—'}</td>
                          {keyCol >= 0 && (
                            <td className="dt-open">
                              {vkey ? (
                                <a
                                  href={href('watch', {
                                    key: vkey,
                                    params: { t: timeOf(res.columns, row, roleOf) },
                                  })}
                                  title="watch this reel at this moment"
                                >
                                  <Play size={11} />
                                </a>
                              ) : null}
                            </td>
                          )}
                          {row.map((v, ci) => (
                            <Cell
                              key={ci}
                              table={table}
                              column={res.columns[ci]}
                              rowid={rowid}
                              value={v}
                              role={roleOf.get(res.columns[ci])?.role}
                            />
                          ))}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="dtable-more">
                <span className="dim">
                  page {page + 1} of {fmtCount(pages)} · click any cell to see where the value came
                  from
                </span>
                <span className="spacer" />
                <code className="sql" title="the request behind this table">
                  {url}
                </code>
                <button
                  className="btn-ghost"
                  onClick={() => void navigator.clipboard?.writeText(url)}
                  title="copy the request URL"
                >
                  <Copy size={11} />
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * The start time on this row, if the table has one — so Play lands in the right
 * place. Two decimals, matching `router.watch()`, so a link built here and a
 * link built from a result are the same string for the same moment.
 */
function timeOf(
  columns: string[],
  row: unknown[],
  roles: Map<string, SchemaColumn>
): string | undefined {
  for (let i = 0; i < columns.length; i += 1) {
    if (roles.get(columns[i])?.role === 'start') {
      const v = row[i];
      if (typeof v === 'number' && Number.isFinite(v)) return v.toFixed(2);
    }
  }
  return undefined;
}

function Cell({
  table,
  column,
  rowid,
  value,
  role,
}: {
  table: string;
  column: string;
  rowid?: number;
  value: unknown;
  role?: SchemaColumn['role'];
}) {
  const isNull = value === null || value === undefined;
  const isNum = typeof value === 'number';
  const text = isNull ? 'null' : String(value);
  // A time column reads as seconds, so show what it means beside the number.
  const hint =
    (role === 'start' || role === 'end') && isNum
      ? fmtT(value as number)
      : column === 'duration' && isNum
        ? fmtDur(value as number)
        : '';

  return (
    <td className={`dt-cell${isNum ? ' is-num' : ''}${isNull ? ' is-null' : ''}`}>
      <button
        className="cell-btn"
        onClick={() =>
          store.openDrill(
            // A rowid when the table has one; the value itself when it does not,
            // because a WITHOUT ROWID table cannot be pointed at any other way.
            typeof rowid === 'number' && rowid > 0
              ? { table, column, rowid }
              : { table, column, value: isNull ? '' : String(value) }
          )
        }
        title={isNull ? 'no value was ever written here' : text}
      >
        {isNull ? <span className="cell-null">null</span> : clip(text, 140)}
        {hint && <span className="cell-hint">{hint}</span>}
      </button>
    </td>
  );
}

function SchemaOverview({ tables, loading }: { tables: SchemaTable[]; loading: boolean }) {
  if (loading) {
    return (
      <div className="sch-grid">
        {Array.from({ length: 6 }, (_, i) => (
          <div className="skel" style={{ height: 150 }} key={i} />
        ))}
      </div>
    );
  }
  if (!tables.length) {
    return (
      <div className="state-box">
        <div className="head">No tables yet</div>
        <div>
          The database has not been written. <a href={href('admin')}>Restore the pinned bundle</a>{' '}
          to get one.
        </div>
      </div>
    );
  }
  return (
    <>
      <div className="view-hint">
        Everything the system believes lives in these tables. Pick one to read it row by
        row — or click a cell anywhere in the app to arrive here pointed at that exact
        value.
      </div>
      <div className="sch-grid">
        {tables.map((t) => (
          <a className="sch" key={t.name} href={href('data', { params: { table: t.name } })}>
            <div className="sch-h">
              <span className="sch-n">{t.name}</span>
              <span className="sch-r">{fmtCount(t.rows)}</span>
            </div>
            <div className="sch-cols">
              {t.columns.slice(0, 14).map((c) => (
                <span
                  key={c.name}
                  className={`sch-c sch-${c.role}`}
                  title={`${c.name} · ${c.type || 'no declared type'} · ${ROLE_NOTE[c.role]}${
                    c.source ? ` · written by ${c.source}` : ''
                  }`}
                >
                  {c.name}
                </span>
              ))}
              {t.columns.length > 14 && (
                <span className="sch-c dim">+{t.columns.length - 14} more</span>
              )}
            </div>
            <div className="sch-f">
              {t.key ? `keyed on ${t.key}` : 'no key column'}
              {t.start ? ` · timed on ${t.start}${t.end ? `–${t.end}` : ''}` : ''}
              {t.indexed ? ' · searchable' : ''}
            </div>
          </a>
        ))}
      </div>
    </>
  );
}

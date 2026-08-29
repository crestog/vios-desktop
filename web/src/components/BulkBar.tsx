/**
 * components/BulkBar.tsx — what you can do to a selection, and only that.
 *
 * The design doc lists "add to collection / requeue / export / open as
 * playlist". Two of those four have no endpoint behind them: there is no
 * `collection` table and no playback queue. A button that looks real and does
 * nothing is worse than an absent one — it teaches you the app is broken — so
 * this bar offers the three the server can actually perform, and says plainly
 * that collections are not built yet rather than leaving a dead control.
 *
 * `enqueueVideo` is called once per key rather than as one batch call, because
 * `/api/engine/enqueue` takes a single `video_key`. Sequential, not
 * `Promise.all`: a hundred parallel POSTs against a single-writer sqlite is how
 * you get "database is locked" for no gain on a local socket.
 */

import { useState } from 'react';
import { Copy, Network, X, Zap } from 'lucide-react';
import { enqueueVideo, prioritizeMirror } from '../lib/api';
import { go } from '../lib/router';
import { fmtCount, plural } from '../lib/format';

export interface BulkBarProps {
  keys: string[];
  onClear: () => void;
}

export default function BulkBar({ keys, onClear }: BulkBarProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);

  const run = async (
    label: string,
    fn: (key: string) => Promise<unknown>,
    // What the whole batch amounted to, when the per-reel answer carries a
    // number. Without it "processed here: all 100" is true and useless — a
    // hundred reels that were already measured report identically to a hundred
    // that just queued a thousand passes.
    tally?: (results: unknown[]) => string
  ) => {
    setBusy(label);
    setSaid(null);
    const done: unknown[] = [];
    const failures: string[] = [];
    for (const k of keys) {
      try {
        done.push(await fn(k));
      } catch (e) {
        failures.push(`${k}: ${String((e as Error).message || e)}`);
      }
    }
    setBusy(null);
    const ok = done.length;
    // The count of failures is reported, not swallowed — "queued 98 of 100" is
    // actionable and "done" is not.
    setSaid(
      failures.length
        ? `${label}: ${ok} of ${keys.length} · ${failures.length} failed — ${failures[0]}`
        : tally
          ? `${label}: ${tally(done)}`
          : `${label}: all ${ok}`
    );
  };

  return (
    <div className="bulkbar" role="toolbar" aria-label="Selection actions">
      <span className="bulk-n">{fmtCount(keys.length)} selected</span>

      <button
        className="btn"
        disabled={busy !== null}
        onClick={() =>
          void run(
            'processed here',
            (k) => enqueueVideo(k),
            (rs) => {
              const rows = rs as Array<{ enqueued: number; already: number }>;
              const q = rows.reduce((a, r) => a + r.enqueued, 0);
              const had = rows.reduce((a, r) => a + r.already, 0);
              return q
                ? `${plural(q, 'pass', 'passes')} queued across ${plural(rows.length, 'reel')}`
                : `nothing to do — ${plural(had, 'pass', 'passes')} already ran`;
            }
          )
        }
        title="queue the local passes these reels are missing"
      >
        <Zap size={12} /> {busy === 'processed here' ? 'queueing…' : 'Process here'}
      </button>

      <button
        className="btn"
        disabled={busy !== null}
        onClick={() => void run('downloading', (k) => prioritizeMirror(k))}
        title="pull these down from the channel before the rest"
      >
        {busy === 'downloading' ? 'queueing…' : 'Download first'}
      </button>

      <button
        className="btn"
        onClick={() => go('graph', { params: { keys: keys.join(',') } })}
        title="build a graph from just these reels"
      >
        <Network size={12} /> In the graph
      </button>

      <button
        className="btn-ghost"
        onClick={() => {
          void navigator.clipboard?.writeText(keys.join('\n'));
          setSaid('keys copied, one per line');
        }}
      >
        <Copy size={11} /> Copy keys
      </button>

      <span className="spacer" />
      {said && <span className="bulk-said">{said}</span>}
      <span className="bulk-note" title="there is no collection table in the database yet">
        collections aren't built yet
      </span>
      <button className="btn-ghost" onClick={onClear} title="clear the selection">
        <X size={12} />
      </button>
    </div>
  );
}

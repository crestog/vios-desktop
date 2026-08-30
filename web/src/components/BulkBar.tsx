/**
 * components/BulkBar.tsx — what you can do to a selection, and only that.
 *
 * The design doc lists "add to collection / requeue / export / open as
 * playlist". Three of the four have an endpoint behind them now; there is still
 * no playback queue, so there is still no playlist button. A button that looks
 * real and does nothing is worse than an absent one — it teaches you the app is
 * broken — so this bar offers exactly what the server can perform.
 *
 * Filing is additive and the label says so, because that is the server's
 * contract rather than this bar's shortcut: `set_collections` never removes a
 * membership, so "File under" adds a shelf and leaves the others alone. A reel
 * already on two shelves ends up on three, which is the entire point of
 * collections being a membership table instead of a column.
 *
 * `enqueueVideo` is called once per key rather than as one batch call, because
 * `/api/engine/enqueue` takes a single `video_key`. Sequential, not
 * `Promise.all`: a hundred parallel POSTs against a single-writer sqlite is how
 * you get "database is locked" for no gain on a local socket. Filing is the
 * opposite shape — one statement about a set — so it is one call.
 */

import { useState } from 'react';
import { Copy, FolderPlus, Network, X, Zap } from 'lucide-react';
import { addToCollection, enqueueVideo, prioritizeMirror } from '../lib/api';
import { go } from '../lib/router';
import { fmtCount, plural } from '../lib/format';

export interface BulkBarProps {
  keys: string[];
  /** Existing shelf names, for the suggestion list. The caller already has them. */
  collections?: string[];
  /** Called after a successful filing, so the grid can show the new chip. */
  onFiled?: () => void;
  onClear: () => void;
}

export default function BulkBar({ keys, collections, onFiled, onClear }: BulkBarProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [shelf, setShelf] = useState('');

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

  const file = async () => {
    const name = shelf.trim();
    if (!name) return;
    setBusy('filed');
    setSaid(null);
    try {
      const r = await addToCollection(name, keys);
      const skipped = r.unknown?.length
        ? ` · ${plural(r.unknown.length, 'key')} the archive does not know`
        : '';
      // `added` counts new memberships and `videos` counts reels asked about, so
      // the two differ when part of the selection was already on that shelf.
      // Saying which is the difference between a report and a shrug.
      setSaid(
        r.added
          ? `filed ${plural(r.added, 'reel')} under "${r.collection}"` +
            (r.added < r.videos ? ` · ${r.videos - r.added} already there` : '') +
            skipped
          : // "all 1 reel were" is what `plural` plus a hard-coded verb produces,
            // and one reel is the commonest selection there is.
            `${r.videos === 1 ? 'that reel is' : `all ${plural(r.videos, 'reel')} are`}` +
            ` already under "${r.collection}"${skipped}`
      );
      setShelf('');
      onFiled?.();
    } catch (e) {
      setSaid(`filing failed — ${String((e as Error).message || e)}`);
    }
    setBusy(null);
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
        onClick={() =>
          void run(
            'downloading',
            (k) => prioritizeMirror(k),
            // `all 100` was a lie here whenever part of the selection was
            // already local or was a reel the mirror had never heard of. The
            // endpoint returns which of those each reel was, so say it.
            (rs) => {
              const rows = rs as Array<{ state: string }>;
              const n = (s: string) => rows.filter((r) => r.state === s).length;
              const parts = [
                n('queued') ? `${n('queued')} queued` : '',
                n('downloading') ? `${n('downloading')} already downloading` : '',
                n('ready') ? `${n('ready')} already here` : '',
                n('deriving') ? `${n('deriving')} being prepared` : '',
                n('unknown') ? `${n('unknown')} not in the channel yet` : '',
              ].filter(Boolean);
              return parts.length ? parts.join(' · ') : `all ${rows.length}`;
            }
          )
        }
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

      {/* An input rather than a menu of existing shelves, because a new shelf is
          as ordinary an action as reusing one — the suggestion list offers what
          exists without making it the only option. Enter files, so the whole
          gesture is select, type, Enter. */}
      <span className="bulk-file">
        <input
          className="input-text"
          list="bulk-shelves"
          value={shelf}
          placeholder="collection…"
          disabled={busy !== null}
          onChange={(e) => setShelf(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void file();
          }}
          aria-label="collection to file the selection under"
        />
        <datalist id="bulk-shelves">
          {(collections || []).map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
        <button
          className="btn"
          disabled={busy !== null || !shelf.trim()}
          onClick={() => void file()}
          title="add these reels to that collection — it never removes the ones they are already in"
        >
          <FolderPlus size={12} /> {busy === 'filed' ? 'filing…' : 'File under'}
        </button>
      </span>

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
      <span
        className="bulk-note"
        title="a playlist needs a playback queue, and there is no endpoint for one yet"
      >
        no playlist yet
      </span>
      <button className="btn-ghost" onClick={onClear} title="clear the selection">
        <X size={12} />
      </button>
    </div>
  );
}

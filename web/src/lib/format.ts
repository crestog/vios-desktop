/**
 * lib/format.ts — every number the user reads, formatted in one place.
 *
 * Not a convenience module. Three of these encode a decision that a view
 * getting it wrong would turn into a lie:
 *
 *   - **`null` is not zero.** A duration that was never probed renders as `—`,
 *     never `0:00`, because `0:00` claims a measurement nobody made.
 *   - **Bytes are binary.** The Python side reports `shutil.disk_usage` and
 *     `os.path.getsize`, which are powers of two, so 1 GB here is 1024³. A
 *     decimal formatter would disagree with Explorer by 7%.
 *   - **Counts are grouped, times are tabular.** `--font-mono` sets
 *     `tabular-nums`, so a status strip that ticks does not shift the layout
 *     next to it.
 */

/** `74` → `1:14`, `3812` → `1:03:32`, `null` → `—`. */
export function fmtDur(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const two = (n: number) => String(n).padStart(2, '0');
  return h ? `${h}:${two(m)}:${two(sec)}` : `${m}:${two(sec)}`;
}

/** A timestamp inside a reel — one decimal, because moments land sub-second. */
export function fmtT(t: number | null | undefined): string {
  if (t === null || t === undefined || !Number.isFinite(t)) return '—';
  const s = Math.max(0, t);
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

export function fmtBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—';
  let n = Math.max(0, bytes);
  let u = 0;
  while (n >= 1024 && u < UNITS.length - 1) {
    n /= 1024;
    u += 1;
  }
  return `${n.toFixed(u === 0 ? 0 : digits)} ${UNITS[u]}`;
}

export const GB = 1024 ** 3;

export function fmtCount(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  return Math.round(n).toLocaleString();
}

/** Compact, for a badge where four digits will not fit: `12.4k`. */
export function fmtCompact(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  const a = Math.abs(n);
  if (a >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}

/**
 * "4 minutes ago". Accepts seconds *or* milliseconds since the epoch —
 * the Python side reports `time.time()` (seconds) and `Date.now()` is
 * milliseconds, and mixing them silently produces "in 55 years".
 */
export function fmtAgo(when: number | null | undefined): string {
  if (!when || !Number.isFinite(when)) return '—';
  const ms = when > 1e12 ? when : when * 1000;
  const delta = Date.now() - ms;
  if (delta < 0) return 'just now';
  const s = Math.floor(delta / 1000);
  if (s < 45) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 18) return `${mo}mo ago`;
  return `${Math.floor(d / 365)}y ago`;
}

/** An absolute date, for a tooltip beside a relative one. */
export function fmtDate(when: number | null | undefined): string {
  if (!when || !Number.isFinite(when)) return '';
  const ms = when > 1e12 ? when : when * 1000;
  return new Date(ms).toLocaleString();
}

/**
 * "in 4h" — the forward-facing counterpart to {@link fmtAgo}.
 *
 * A separate function rather than a sign check inside `fmtAgo`, because the two
 * answer different questions and the wrong one reads as a bug: a capture row
 * parked until tomorrow is *scheduled*, and rendering that as "23h ago" (or as
 * `fmtAgo`'s "just now" for any future time) says the retry already happened.
 * Same seconds-or-milliseconds tolerance, for the same reason.
 */
export function fmtIn(when: number | null | undefined): string {
  if (!when || !Number.isFinite(when)) return '';
  const ms = when > 1e12 ? when : when * 1000;
  const s = Math.round((ms - Date.now()) / 1000);
  if (s <= 0) return 'due now';
  if (s < 90) return `in ${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `in ${m}m`;
  const h = Math.round(s / 3600);
  if (h < 48) return `in ${h}h`;
  return `in ${Math.round(s / 86400)}d`;
}

export function fmtPct(part: number, whole: number, digits = 0): string {
  if (!whole) return '—';
  return `${((part / whole) * 100).toFixed(digits)}%`;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/** Collapse whitespace and clip, for a transcript excerpt in a list row. */
export function clip(text: string | null | undefined, max = 220): string {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/**
 * components/Spectrum.tsx — the signature element, built once and used twice.
 *
 * A band where **x is time in the reel, colour is which channel fired, and
 * height is relative strength.** Four pixels tall it sits under a card; forty
 * pixels tall and interactive it is the player's marker track. One component
 * for both, because two copies would drift and the whole point is that the
 * shape under a search result and the shape under the video are the same shape.
 *
 * Three things it refuses to fake, all of which the data forces:
 *
 *   - **A moment with no timings is not dropped and not given a position.**
 *     `t_start` is null for every claim about the whole reel — a caption, a
 *     style read, a concept. Those render as a full-width wash *behind* the
 *     placed moments, which is exactly what they are: true of all of it.
 *
 *   - **A reel with no duration has no timeline.** If neither `duration` nor
 *     any `t_end` is on record there is nothing to place moments against, so
 *     they are drawn as equal slices at reduced height and the tooltip says
 *     "no timings recorded". Equal slices that *looked* like a timeline would
 *     be the single most misleading thing this component could do.
 *
 *   - **`score` is not confidence.** `/api/search` returns a fused rank score
 *     on no fixed scale, so height is normalised *within the set handed in* and
 *     means "relative strength among these hits", never "the model was 80%
 *     sure". When nothing carries a score, every bar is full height rather than
 *     invented.
 */

import { useMemo } from 'react';
import type { Moment } from '../types';
import { CHANNEL_MEANING, CHANNEL_VAR, channelOf } from '../lib/channels';
import { clip, fmtT } from '../lib/format';

export interface SpectrumProps {
  moments?: Moment[];
  /** Seconds. `null` when the reel was never probed — handled, not assumed. */
  duration: number | null;
  height?: number;
  /** The playhead. Only the player passes this. */
  at?: number | null;
  /** When present the band is interactive and reports a seek target. */
  onSeek?: (t: number) => void;
  /** The id of the strongest passage, drawn brighter. Search sets this. */
  bestId?: number;
  className?: string;
}

interface Band {
  key: string;
  left: number;
  width: number;
  h: number;
  colour: string;
  title: string;
  whole: boolean;
  best: boolean;
}

/** Below this a bar is invisible, so a 0.2 s OCR hit gets a minimum presence. */
const MIN_PCT = 0.9;

function tooltip(m: Moment, whole: boolean): string {
  const ch = channelOf(m.source || m.src_table);
  const when = whole ? 'whole reel' : `${fmtT(m.t_start)} → ${fmtT(m.t_end ?? m.t_start)}`;
  const text = clip(m.text, 120);
  return `${ch} · ${when}${text ? `\n${text}` : ''}\n${CHANNEL_MEANING[ch]}`;
}

export default function Spectrum({
  moments,
  duration,
  height = 4,
  at,
  onSeek,
  bestId,
  className,
}: SpectrumProps) {
  const { bands, span, timeless } = useMemo(() => {
    const list = moments || [];
    if (!list.length) return { bands: [] as Band[], span: 0, timeless: false };

    // The timeline: the probed duration when there is one, otherwise the last
    // moment that carries an end. Using the observed maximum is not a guess —
    // it is the latest point evidence actually exists at.
    let observed = 0;
    for (const m of list) {
      const end = m.t_end ?? m.t_start;
      if (end !== null && end !== undefined && Number.isFinite(end)) {
        observed = Math.max(observed, end);
      }
    }
    const timeSpan = duration && duration > 0 ? duration : observed;
    const noTimeline = !(timeSpan > 0);

    // Normalise within the set. `null` here means "nothing carried a score",
    // which is different from "every score was zero".
    let top: number | null = null;
    for (const m of list) {
      if (typeof m.score === 'number' && Number.isFinite(m.score)) {
        top = top === null ? m.score : Math.max(top, m.score);
      }
    }
    const strength = (m: Moment): number => {
      if (top === null || !top) return 1;
      const s = typeof m.score === 'number' && Number.isFinite(m.score) ? m.score : 0;
      // Floor at 0.45: a weak hit is still a hit, and a 4 px band at 8% height
      // is a rendering artefact rather than information.
      return 0.45 + 0.55 * Math.min(1, Math.max(0, s / top));
    };

    if (noTimeline) {
      const w = 100 / list.length;
      return {
        span: 0,
        timeless: true,
        bands: list.map((m, i) => ({
          key: `${m.id}-${i}`,
          left: i * w,
          width: Math.max(MIN_PCT, w - 0.6),
          h: 0.4,
          colour: CHANNEL_VAR[channelOf(m.source || m.src_table)],
          title: `${tooltip(m, true)}\n(no timings recorded — position here is not time)`,
          whole: true,
          best: m.id === bestId,
        })),
      };
    }

    const out: Band[] = [];
    for (let i = 0; i < list.length; i += 1) {
      const m = list[i];
      const ch = channelOf(m.source || m.src_table);
      const colour = CHANNEL_VAR[ch];
      const hasStart = m.t_start !== null && m.t_start !== undefined && Number.isFinite(m.t_start);
      if (!hasStart) {
        out.push({
          key: `${m.id}-${i}`,
          left: 0,
          width: 100,
          h: strength(m),
          colour,
          title: tooltip(m, true),
          whole: true,
          best: m.id === bestId,
        });
        continue;
      }
      const t0 = Math.max(0, Math.min(timeSpan, m.t_start as number));
      const rawEnd = m.t_end ?? t0;
      const t1 = Math.max(t0, Math.min(timeSpan, rawEnd));
      const left = (t0 / timeSpan) * 100;
      out.push({
        key: `${m.id}-${i}`,
        left,
        width: Math.max(MIN_PCT, Math.min(100 - left, ((t1 - t0) / timeSpan) * 100)),
        h: strength(m),
        colour,
        title: tooltip(m, false),
        whole: false,
        best: m.id === bestId,
      });
    }
    // Whole-reel washes first so placed moments paint over them.
    out.sort((a, b) => Number(b.whole) - Number(a.whole));
    return { bands: out, span: timeSpan, timeless: false };
  }, [moments, duration, bestId]);

  const interactive = Boolean(onSeek) && span > 0;

  const seekFrom = (clientX: number, el: HTMLElement) => {
    if (!onSeek || !(span > 0)) return;
    const box = el.getBoundingClientRect();
    if (!box.width) return;
    const frac = Math.min(1, Math.max(0, (clientX - box.left) / box.width));
    onSeek(frac * span);
  };

  return (
    <div
      className={`spectrum${interactive ? ' spectrum-live' : ''}${className ? ` ${className}` : ''}`}
      style={{ height }}
      role={interactive ? 'slider' : 'img'}
      aria-label={
        bands.length
          ? `${bands.length} evidence ${bands.length === 1 ? 'moment' : 'moments'}${
              timeless ? ', no timings recorded' : ''
            }`
          : 'no evidence yet'
      }
      aria-valuemin={interactive ? 0 : undefined}
      aria-valuemax={interactive ? Math.round(span) : undefined}
      aria-valuenow={interactive && at ? Math.round(at) : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive ? (e) => seekFrom(e.clientX, e.currentTarget) : undefined}
      onKeyDown={
        interactive
          ? (e) => {
              if (!onSeek) return;
              const step = e.shiftKey ? 10 : 1;
              if (e.key === 'ArrowRight') {
                e.preventDefault();
                onSeek(Math.min(span, (at || 0) + step));
              } else if (e.key === 'ArrowLeft') {
                e.preventDefault();
                onSeek(Math.max(0, (at || 0) - step));
              }
            }
          : undefined
      }
    >
      {bands.map((b) => (
        <span
          key={b.key}
          className={`sp-band${b.whole ? ' sp-whole' : ''}${b.best ? ' sp-best' : ''}`}
          title={b.title}
          style={{
            left: `${b.left}%`,
            width: `${b.width}%`,
            height: `${Math.round(b.h * 100)}%`,
            background: b.colour,
          }}
        />
      ))}
      {at !== null && at !== undefined && span > 0 && (
        <span
          className="sp-head"
          style={{ left: `${Math.min(100, Math.max(0, (at / span) * 100))}%` }}
        />
      )}
    </div>
  );
}

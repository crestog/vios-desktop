/**
 * components/FrameHits.tsx — the image lane, which counts in frames.
 *
 * Deliberately not folded into the video grid. `/api/vsearch` returns one row
 * per *frame* — `{video_key, frame_idx, t, score}` — and several rows are
 * usually the same reel at different seconds. Rendering that as video cards
 * would have to either collapse the frames (throwing away the answer: *which*
 * frame looks like this) or repeat the same card six times. So frames get their
 * own unit of result, and the reel is a label on the tile rather than the tile.
 *
 * Two honesty rules, both load-bearing:
 *
 *   - **`t` may be null**, and then the tile says so instead of guessing. The
 *     frame index is exact; the seconds are derived from a frame rate the
 *     server refuses to invent at 30 fps. A tile with no `t` still opens the
 *     reel — just not at a timestamp nobody measured.
 *
 *   - **`score` is a distance in an embedding space, not a probability.** The
 *     bar is normalised across the hits on screen and labelled "relative", so
 *     nobody reads 0.31 as 31% confident.
 */

import { useMemo } from 'react';
import { frameUrl, type VisualHit } from '../lib/api';
import { href } from '../lib/router';
import { fmtT, plural } from '../lib/format';

export interface FrameHitsProps {
  hits: VisualHit[];
  /** Why the list is empty — four causes look identical without this. */
  reason?: string;
  error?: string | null;
  first?: boolean;
  loading?: boolean;
  q?: string;
}

export default function FrameHits({ hits, reason, error, first, loading, q }: FrameHitsProps) {
  const { lo, span, reels } = useMemo(() => {
    const scores = hits.map((h) => h.score).filter((s) => Number.isFinite(s));
    const min = scores.length ? Math.min(...scores) : 0;
    const max = scores.length ? Math.max(...scores) : 1;
    return {
      lo: min,
      span: max - min || 1,
      reels: new Set(hits.map((h) => h.video_key)).size,
    };
  }, [hits]);

  if (error) {
    return (
      <div className="state-box err">
        <div className="head">The image index did not answer</div>
        <div>{error}</div>
      </div>
    );
  }

  if (first && loading) {
    return (
      <div className="skel-grid" style={{ gridTemplateColumns: 'repeat(6, 1fr)' }}>
        {Array.from({ length: 18 }, (_, i) => (
          <div className="skel skel-card" key={i} />
        ))}
      </div>
    );
  }

  if (!hits.length) {
    return (
      <div className="state-box">
        <div className="head">No frames matched</div>
        <div>
          {reason ||
            'Either nothing in the frames resembles that, or the image index has not been built for these reels yet.'}
        </div>
      </div>
    );
  }

  return (
    <div className="view-body">
      <div className="frames-note">
        {plural(hits.length, 'frame')} across {plural(reels, 'reel')} · the bar is each frame's
        strength <em>relative to the others here</em>, not a confidence
      </div>
      <div className="frames">
        {hits.map((h) => {
          const strength = 0.35 + 0.65 * ((h.score - lo) / span);
          return (
            <a
              className="frame-tile"
              key={`${h.video_key}:${h.frame_idx}`}
              href={href('watch', {
                key: h.video_key,
                params: { t: h.t !== null ? h.t.toFixed(2) : undefined, q },
              })}
              title={
                h.t === null
                  ? `frame ${h.frame_idx} — this reel has no frame rate on record, so the second it lands on is unknown`
                  : `frame ${h.frame_idx} at ${fmtT(h.t)}`
              }
            >
              <img
                className="frame-img"
                src={frameUrl(h.video_key, { i: h.frame_idx })}
                alt={`frame ${h.frame_idx}`}
                loading="lazy"
                decoding="async"
                draggable={false}
              />
              <span className="frame-bar" style={{ width: `${(strength * 100).toFixed(1)}%` }} />
              <span className="frame-foot">
                <span className="frame-t">{h.t === null ? `#${h.frame_idx}` : fmtT(h.t)}</span>
                <span className="frame-space">{h.space}</span>
              </span>
            </a>
          );
        })}
      </div>
    </div>
  );
}

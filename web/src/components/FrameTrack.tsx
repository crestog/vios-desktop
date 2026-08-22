/**
 * components/FrameTrack.tsx — the frames, full width, big enough to actually see.
 *
 * This component is the direct answer to the v17 complaint: *"the place where
 * frames were shown was getting so small that anything was not visible."* The
 * cause was putting the frames in a **column** beside the video, where they got
 * whatever width was left over — about 90 px on a laptop screen. So here they
 * are a **row**: one horizontal scroller across the full width of the player,
 * each frame a fixed 108 px wide (16:9-agnostic — reels are 9:16, so a frame is
 * taller than it is wide and 108 px reads clearly at arm's length).
 *
 * Three behaviours worth knowing:
 *
 *   - **The strip follows the playhead, but only when it is off screen.** A
 *     scroller that re-centres on every tick fights the pointer while you are
 *     dragging through it, so the scroll only happens when the active frame is
 *     outside the visible range.
 *
 *   - **`t` may be null.** `KeyframeIndex` frames carry a null `t` when the reel
 *     has no frame rate on record: the index is exact, the seconds are derived.
 *     A frame with no `t` is shown but not clickable-to-seek, and says why.
 *
 *   - **`capped` is surfaced.** The extractor stops at a per-video frame cap, so
 *     a long reel's strip can end well before the reel does. Silently showing 200
 *     frames of a 400-frame video would read as "that is all there is".
 */

import { memo, useEffect, useMemo, useRef } from 'react';
import { keyframeUrl } from '../lib/api';
import { fmtT } from '../lib/format';

export interface FrameTrackProps {
  videoKey: string;
  frames?: Array<{ i: number; file: string; t: number | null }>;
  capped?: boolean;
  error?: string | null;
  loading?: boolean;
  at: number;
  duration: number | null;
  onSeek: (t: number) => void;
}

const FRAME_W = 108;

function FrameTrackInner({
  videoKey,
  frames,
  capped,
  error,
  loading,
  at,
  duration,
  onSeek,
}: FrameTrackProps) {
  const strip = useRef<HTMLDivElement | null>(null);

  // Which frame the playhead is inside. Nearest-preceding rather than nearest,
  // because a frame is the start of a span, not a point.
  const active = useMemo(() => {
    if (!frames || !frames.length) return -1;
    let best = -1;
    for (let i = 0; i < frames.length; i += 1) {
      const t = frames[i].t;
      if (t === null) continue;
      if (t <= at + 0.001) best = i;
      else break;
    }
    return best;
  }, [frames, at]);

  useEffect(() => {
    const el = strip.current;
    if (!el || active < 0) return;
    const left = active * (FRAME_W + 6);
    const right = left + FRAME_W;
    if (left < el.scrollLeft || right > el.scrollLeft + el.clientWidth) {
      el.scrollTo({ left: Math.max(0, left - el.clientWidth / 2), behavior: 'smooth' });
    }
  }, [active]);

  if (error) {
    return (
      <div className="ftrack ftrack-none">
        Frames have not been extracted for this reel yet — {error}
      </div>
    );
  }

  if (loading) {
    return (
      <div className="ftrack">
        {Array.from({ length: 12 }, (_, i) => (
          <div className="skel ftrack-cell" key={i} />
        ))}
      </div>
    );
  }

  if (!frames || !frames.length) {
    return (
      <div className="ftrack ftrack-none">
        No frames on record for this reel. The mirror extracts them in the same pass
        that writes the proxy, so this fills in once it reaches this video.
      </div>
    );
  }

  return (
    <>
      <div className="ftrack" ref={strip} aria-label="Frames">
        {frames.map((f, i) => (
          <button
            className={`ftrack-cell${i === active ? ' is-active' : ''}`}
            key={f.i}
            style={{ width: FRAME_W }}
            onClick={() => (f.t !== null ? onSeek(f.t) : undefined)}
            disabled={f.t === null}
            title={
              f.t === null
                ? `frame ${f.i} — this reel has no frame rate on record, so its timestamp is unknown`
                : `frame ${f.i} at ${fmtT(f.t)}`
            }
          >
            <img
              src={keyframeUrl(videoKey, f.file)}
              alt={`frame ${f.i}`}
              loading="lazy"
              decoding="async"
              draggable={false}
            />
            <span className="ftrack-t">{f.t === null ? `#${f.i}` : fmtT(f.t)}</span>
          </button>
        ))}
      </div>
      {capped && (
        <div className="ftrack-note">
          The extractor hit its per-reel frame cap, so this strip stops before the reel
          does{duration ? ` (${fmtT(frames[frames.length - 1]?.t ?? 0)} of ${fmtT(duration)})` : ''}.
        </div>
      )}
    </>
  );
}

/**
 * Memoised on purpose. The playhead updates twenty times a second, and without
 * this every tick re-renders up to two hundred `<img>` elements to move one
 * highlight. `at` is still in the props, so it re-renders when the *active
 * frame* changes — which is what the highlight needs and nothing more.
 */
export default memo(FrameTrackInner, (a, b) => {
  if (a.videoKey !== b.videoKey || a.frames !== b.frames) return false;
  if (a.error !== b.error || a.loading !== b.loading || a.capped !== b.capped) return false;
  if (a.duration !== b.duration || a.onSeek !== b.onSeek) return false;
  // Re-render only when `at` crosses into a different frame.
  const idx = (t: number) => {
    const f = a.frames;
    if (!f || !f.length) return -1;
    let best = -1;
    for (let i = 0; i < f.length; i += 1) {
      const ft = f[i].t;
      if (ft === null) continue;
      if (ft <= t + 0.001) best = i;
      else break;
    }
    return best;
  };
  return idx(a.at) === idx(b.at);
});

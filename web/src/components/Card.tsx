/**
 * components/Card.tsx — one result, in every view that shows results.
 *
 * It is an `<a href>`, not a div with an onClick, and that is load-bearing:
 * middle-click opens a second window, right-click offers "copy link address",
 * and the router intercepts the plain left click. A div would silently lose all
 * three, and the player being a real route is most of the reason this app has
 * URLs at all.
 *
 * Four behaviours worth reading before changing:
 *
 *   1. **The poster tier follows density, not CSS.** A twelve-across contact
 *      sheet asks for 160 px JPEGs. Scaling a 720 px poster down in CSS fetches
 *      roughly twenty times the bytes for pixels nobody can resolve, which is
 *      the difference between a grid that scrolls at 60 fps and one that
 *      stutters on every new row.
 *
 *   2. **Two poster sources, in order.** `/api/derived/poster` is a file the
 *      mirror already wrote — instant. `/api/poster` extracts a frame on
 *      demand — correct but slower, and it works for a video that has not been
 *      derived yet. The derived one is tried first and the on-demand one is the
 *      `onError` fallback, so a half-mirrored archive still shows covers.
 *
 *   3. **Hover waits 200 ms before asking for anything.** Moving the mouse
 *      across a twelve-column grid crosses a dozen cards; firing a clip request
 *      per card would queue twelve video fetches to play none of them.
 *
 *   4. **The clip crossfades in over 90 ms.** The swap only reads as
 *      intentional if the poster is still there underneath while the first
 *      frame arrives — cutting to a black `<video>` element for two frames
 *      reads as a bug, every time.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { VideoItem } from '../types';
import { clipUrl, framePosterUrl, posterUrl } from '../lib/api';
import { channelsIn, chipClass } from '../lib/channels';
import { fmtCompact, fmtDur, fmtT } from '../lib/format';
import { href } from '../lib/router';
import Spectrum from './Spectrum';

export interface CardProps {
  video: VideoItem;
  /** 3–12. Chooses the poster tier and how much text there is room for. */
  density?: number;
  /** Carried into the player URL so the moment keeps its query context. */
  q?: string;
  /** Off for the filmstrip, where a hundred lanes hovering at once is noise. */
  hoverClip?: boolean;
  /** Contact-sheet mode drops the footer at any density. Default: `density <= 9`. */
  showText?: boolean;
  selected?: boolean;
  onSelectToggle?: (key: string, e: React.MouseEvent) => void;
}

/** Density → poster tier. The three tiers the deriver actually writes. */
export function tierFor(density: number): 160 | 360 | 720 {
  if (density >= 8) return 160;
  if (density >= 5) return 360;
  return 720;
}

const HOVER_DELAY_MS = 200;

export default function Card({
  video,
  density = 5,
  q,
  hoverClip = true,
  showText,
  selected,
  onSelectToggle,
}: CardProps) {
  const key = video.video_key;
  const tier = tierFor(density);
  const [src, setSrc] = useState(() => posterUrl(key, tier));
  const [fellBack, setFellBack] = useState(false);
  const [clipSrc, setClipSrc] = useState<string | null>(null);
  const [clipUp, setClipUp] = useState(false);
  const timer = useRef<number | null>(null);
  const vid = useRef<HTMLVideoElement | null>(null);

  // Density can change under a mounted card (the slider), and the tier has to
  // follow. Reset the fallback flag too — the derived poster may exist at the
  // new tier even if it did not at the old one.
  useEffect(() => {
    setSrc(posterUrl(key, tier));
    setFellBack(false);
  }, [key, tier]);

  const matchedT = video.best?.t_start ?? null;
  const channels = channelsIn(video.moments || (video.best ? [video.best] : undefined));
  const moments = video.moments || (video.best ? [video.best] : []);

  const clearTimer = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  };

  useEffect(() => clearTimer, []);

  const onEnter = useCallback(() => {
    if (!hoverClip || video.has_file === false) return;
    clearTimer();
    timer.current = window.setTimeout(() => {
      setClipSrc(clipUrl(key, matchedT ?? 0));
    }, HOVER_DELAY_MS);
  }, [hoverClip, key, matchedT, video.has_file]);

  const onLeave = useCallback(() => {
    clearTimer();
    setClipUp(false);
    // Drop the element rather than pausing it: a paused <video> holds a decoder
    // and its buffer, and a grid the mouse has crossed would hold dozens.
    setClipSrc(null);
    const el = vid.current;
    if (el) {
      el.pause();
      el.removeAttribute('src');
      el.load();
    }
  }, []);

  const withText = showText ?? density <= 9;

  return (
    <a
      className={`vios-card${selected ? ' is-selected' : ''}`}
      href={href('watch', {
        key,
        params: { t: matchedT !== null ? matchedT.toFixed(2) : undefined, q },
      })}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      onClick={(e) => {
        // Ctrl/Cmd-click is multi-select where a view offers it, and must not
        // navigate. Without the guard the router would open the player instead.
        if (onSelectToggle && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          e.stopPropagation();
          onSelectToggle(key, e);
        }
      }}
      title={video.title || key}
    >
      <div className="card-media">
        <img
          className="card-poster"
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => {
            if (fellBack) return;
            setFellBack(true);
            setSrc(framePosterUrl(key, matchedT ?? undefined));
          }}
        />
        {clipSrc && (
          <video
            ref={vid}
            className="card-clip"
            style={{ opacity: clipUp ? 1 : 0 }}
            src={clipSrc}
            muted
            loop
            playsInline
            autoPlay
            preload="none"
            onPlaying={() => setClipUp(true)}
            onError={() => setClipSrc(null)}
          />
        )}

        {video.has_file === false && (
          <span className="card-flag" title="in the channel, not on this disk yet">
            remote
          </span>
        )}
        {matchedT !== null && (
          <span className="card-t" title="the moment that matched">
            {fmtT(matchedT)}
          </span>
        )}
        {video.duration !== null && video.duration !== undefined && (
          <span className="card-dur">{fmtDur(video.duration)}</span>
        )}
        {typeof video.hit_count === 'number' && video.hit_count > 1 && (
          <span className="card-hits" title={`${video.hit_count} passages matched`}>
            ×{video.hit_count}
          </span>
        )}
      </div>

      <Spectrum
        moments={moments}
        duration={video.duration}
        bestId={video.best?.id}
        height={4}
      />

      {withText && (
        <div className="card-foot">
          <div className="card-title">{video.title || key}</div>
          <div className="card-line">
            {video.creator && <span className="card-creator">{video.creator}</span>}
            {typeof video.likes === 'number' && (
              <span className="card-likes">♥ {fmtCompact(video.likes)}</span>
            )}
            {typeof video.moment_count === 'number' && video.moment_count > 0 && (
              <span className="card-moments">{fmtCompact(video.moment_count)} claims</span>
            )}
          </div>
          {channels.length > 0 && (
            <div className="card-chips">
              {channels.map((c) => (
                <span key={c} className={chipClass(c)}>
                  {c}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </a>
  );
}

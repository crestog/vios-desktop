/**
 * components/ResultRows.tsx — list mode: the one where you read.
 *
 * The grid answers "which reel"; this answers "which *moment*, and what does it
 * actually say". So a row is not one link — it is a video header plus one line
 * per matched passage, each with its own timestamp and its own link into the
 * player at that second. A search that hit six passages in one reel gives six
 * ways in, which is the entire difference between browsing and finding.
 *
 * Not virtualized, deliberately. Rows are variable height (a transcript line
 * wraps, a caption does not), and measuring them to window them costs more than
 * it saves at the page sizes this view uses — you read forty rows, you do not
 * scroll five thousand. The grid is where the five thousand live.
 */

import { useCallback, useRef } from 'react';
import type { Moment, VideoItem } from '../types';
import { framePosterUrl, posterUrl } from '../lib/api';
import { channelOf, chipClass } from '../lib/channels';
import { clip, fmtCompact, fmtDur, fmtT } from '../lib/format';
import { href } from '../lib/router';
import Mark from './Mark';
import Spectrum from './Spectrum';

export interface ResultRowsProps {
  items: VideoItem[];
  q?: string;
  /** How many passages to show per video before "+ n more". */
  perVideo?: number;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  footer?: React.ReactNode;
}

/** Time order, and nulls last — a whole-reel claim has no place in a timeline. */
function inTimeOrder(moments: Moment[]): Moment[] {
  return [...moments].sort((a, b) => {
    const A = a.t_start;
    const B = b.t_start;
    if (A === null || A === undefined) return B === null || B === undefined ? 0 : 1;
    if (B === null || B === undefined) return -1;
    return A - B;
  });
}

export default function ResultRows({
  items,
  q,
  perVideo = 4,
  onLoadMore,
  loadingMore,
  footer,
}: ResultRowsProps) {
  const scroller = useRef<HTMLDivElement | null>(null);

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el || !onLoadMore || loadingMore) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < el.clientHeight) onLoadMore();
  }, [onLoadMore, loadingMore]);

  return (
    <div className="rows" ref={scroller} onScroll={onScroll}>
      {items.map((v) => {
        const all = inTimeOrder(v.moments || (v.best ? [v.best] : []));
        const shown = all.slice(0, perVideo);
        const extra = all.length - shown.length;
        return (
          <article className="rrow" key={v.video_key}>
            <a
              className="rrow-thumb"
              href={href('watch', {
                key: v.video_key,
                params: { t: v.best?.t_start?.toFixed(2), q },
              })}
              title={v.title || v.video_key}
            >
              <img
                src={posterUrl(v.video_key, 160)}
                alt=""
                loading="lazy"
                decoding="async"
                onError={(e) => {
                  const img = e.currentTarget;
                  if (img.dataset.fell) return;
                  img.dataset.fell = '1';
                  img.src = framePosterUrl(v.video_key, v.best?.t_start ?? undefined);
                }}
              />
              {v.duration !== null && v.duration !== undefined && (
                <span className="rrow-dur">{fmtDur(v.duration)}</span>
              )}
            </a>

            <div className="rrow-main">
              <header className="rrow-head">
                <a
                  className="rrow-title"
                  href={href('watch', {
                    key: v.video_key,
                    params: { t: v.best?.t_start?.toFixed(2), q },
                  })}
                >
                  {v.title || v.video_key}
                </a>
                <span className="rrow-meta">
                  {v.creator && <span>{v.creator}</span>}
                  {v.category && <span>{v.category}</span>}
                  {typeof v.likes === 'number' && <span>♥ {fmtCompact(v.likes)}</span>}
                  {typeof v.hit_count === 'number' && v.hit_count > 0 && (
                    <span>
                      {v.hit_count} {v.hit_count === 1 ? 'match' : 'matches'}
                    </span>
                  )}
                </span>
              </header>

              <Spectrum
                moments={all}
                duration={v.duration}
                bestId={v.best?.id}
                height={5}
                className="rrow-spectrum"
              />

              <ul className="rrow-passages">
                {shown.map((m, i) => {
                  const ch = channelOf(m.source || m.src_table);
                  const t = m.t_start;
                  return (
                    <li key={`${m.id}-${i}`}>
                      <a
                        className="passage"
                        href={href('watch', {
                          key: v.video_key,
                          params: { t: t !== null && t !== undefined ? t.toFixed(2) : undefined, q },
                        })}
                      >
                        <span className={chipClass(ch)}>{ch}</span>
                        <span className="passage-t">
                          {t === null || t === undefined ? 'all' : fmtT(t)}
                        </span>
                        <span className="passage-text">
                          <Mark text={clip(m.text, 260)} q={q} />
                        </span>
                      </a>
                    </li>
                  );
                })}
                {extra > 0 && (
                  <li className="rrow-more">
                    <a
                      href={href('watch', {
                        key: v.video_key,
                        params: { t: v.best?.t_start?.toFixed(2), q },
                      })}
                    >
                      + {extra} more in this reel
                    </a>
                  </li>
                )}
                {!all.length && typeof v.moment_count === 'number' && (
                  <li className="rrow-more">{fmtCompact(v.moment_count)} claims on record</li>
                )}
              </ul>
            </div>
          </article>
        );
      })}
      {footer && <div className="rows-footer">{footer}</div>}
    </div>
  );
}

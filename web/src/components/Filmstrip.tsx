/**
 * components/Filmstrip.tsx — the same results, grouped by who made them.
 *
 * This mode exists for one question the grid cannot answer: *whose* reels are
 * these? A flat grid of forty hits sorted by relevance hides the fact that
 * eleven of them are one creator. Lanes make that the first thing you see, and
 * the lane header is the shortcut to "just theirs" — a search filtered to that
 * creator, which is the move you actually want next.
 *
 * Lanes are ordered by size, not alphabetically. Alphabetical order buries the
 * signal: the reason to open this view is to see who dominates the result set.
 */

import { useMemo } from 'react';
import type { VideoItem } from '../types';
import { fmtCompact } from '../lib/format';
import { href } from '../lib/router';
import Card from './Card';

export interface FilmstripProps {
  items: VideoItem[];
  q?: string;
  /** Card width in a lane. Lanes scroll horizontally, so this is fixed. */
  cardW?: number;
  /** Where a lane header links: search keeps the query, library drops it. */
  laneView?: 'search' | 'library';
  onLoadMore?: () => void;
  loadingMore?: boolean;
  footer?: React.ReactNode;
}

const UNKNOWN = 'unattributed';

export default function Filmstrip({
  items,
  q,
  cardW = 168,
  laneView = 'search',
  footer,
}: FilmstripProps) {
  const lanes = useMemo(() => {
    const by = new Map<string, VideoItem[]>();
    for (const v of items) {
      const who = (v.creator || '').trim() || UNKNOWN;
      const lane = by.get(who);
      if (lane) lane.push(v);
      else by.set(who, [v]);
    }
    return [...by.entries()]
      .map(([creator, vids]) => ({ creator, vids }))
      .sort((a, b) => b.vids.length - a.vids.length || a.creator.localeCompare(b.creator));
  }, [items]);

  return (
    <div className="strips">
      {lanes.map(({ creator, vids }) => (
        <section className="strip" key={creator}>
          <header className="strip-head">
            {creator === UNKNOWN ? (
              <span className="strip-who strip-none" title="no creator on record">
                {UNKNOWN}
              </span>
            ) : (
              <a
                className="strip-who"
                href={href(laneView, {
                  params: laneView === 'search' ? { q, creator } : { creator },
                })}
                title={`everything by ${creator}`}
              >
                {creator}
              </a>
            )}
            <span className="strip-n">{fmtCompact(vids.length)}</span>
          </header>
          <div className="strip-lane">
            {vids.map((v) => (
              <div className="strip-cell" key={v.video_key} style={{ width: cardW }}>
                <Card video={v} density={6} q={q} showText />
              </div>
            ))}
          </div>
        </section>
      ))}
      {footer && <div className="rows-footer">{footer}</div>}
    </div>
  );
}

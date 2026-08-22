/**
 * components/VirtualGrid.tsx — five thousand posters, a few dozen DOM nodes.
 *
 * The whole grid is one absolutely-positioned layer inside a scroller of the
 * full computed height, and only the rows inside the viewport (plus two above
 * and two below) exist as elements. Without this, a 5,000-result library is
 * 5,000 `<img>` tags: the browser decodes what it can, the layout pass costs
 * tens of milliseconds, and scrolling drops frames on every new row.
 *
 * Two details that are easy to get wrong and expensive to debug:
 *
 *   - **Item height is computed, not guessed.** A card is a 9:16 poster, a 4 px
 *     spectrum, and a fixed-height footer, so the height follows from the
 *     column width. `FOOT_H` matching the CSS is what keeps the last row from
 *     being clipped — if the footer is ever allowed to wrap, the maths and the
 *     layout disagree and cards start overlapping.
 *
 *   - **Scroll handling is passive and unthrottled.** Unthrottled because the
 *     handler does two multiplications and a `setState` that usually bails on
 *     an unchanged value; a `requestAnimationFrame` wrapper here would add a
 *     frame of latency to buy nothing. Passive because a non-passive scroll
 *     listener blocks the compositor on some Chromium builds.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { VideoItem } from '../types';
import { useSize } from '../lib/useFetch';
import Card from './Card';

export interface VirtualGridProps {
  items: VideoItem[];
  density: number;
  q?: string;
  /** Called once when the scroller is within two screens of the end. */
  onLoadMore?: () => void;
  /** True while a page is in flight, so `onLoadMore` is not asked twice. */
  loadingMore?: boolean;
  /** Contact-sheet mode: posters only. Default follows density. */
  showText?: boolean;
  selected?: Set<string>;
  onSelectToggle?: (key: string, e: React.MouseEvent) => void;
  /** Rendered under the last row — "showing 200 of 4,812", a spinner, an end. */
  footer?: React.ReactNode;
}

const GAP = 10;
const PAD = 14;
/** Must match `.card-foot` in main.css. See the note above. */
const FOOT_H = 70;
const SPECTRUM_H = 4;
const BUFFER_ROWS = 2;

export default function VirtualGrid({
  items,
  density,
  q,
  onLoadMore,
  loadingMore,
  showText,
  selected,
  onSelectToggle,
  footer,
}: VirtualGridProps) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const { w } = useSize(scroller);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(0);

  const cols = Math.max(1, Math.min(12, Math.round(density)));
  const itemW = w > 0 ? Math.floor((w - PAD * 2 - GAP * (cols - 1)) / cols) : 0;
  const withText = showText ?? density <= 9;
  const itemH = itemW > 0 ? Math.round(itemW * (16 / 9)) + SPECTRUM_H + (withText ? FOOT_H : 0) : 0;
  const rowH = itemH + GAP;
  const rows = Math.ceil(items.length / cols);
  const totalH = rows * rowH;

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    setScrollTop(el.scrollTop);
    setViewH(el.clientHeight);
    if (onLoadMore && !loadingMore) {
      const remaining = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (remaining < el.clientHeight * 2) onLoadMore();
    }
  }, [onLoadMore, loadingMore]);

  // A short result set can leave the scroller shorter than its viewport, in
  // which case no scroll event will ever fire and `onLoadMore` would never be
  // reached even though there are more pages. Check once after every render
  // that changes the item count.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    setViewH(el.clientHeight);
    if (onLoadMore && !loadingMore && items.length && el.scrollHeight <= el.clientHeight + 4) {
      onLoadMore();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items.length, w]);

  if (!items.length) return <div ref={scroller} className="vgrid" />;

  const first = rowH > 0 ? Math.max(0, Math.floor(scrollTop / rowH) - BUFFER_ROWS) : 0;
  const visibleRows = rowH > 0 ? Math.ceil((viewH || 800) / rowH) + BUFFER_ROWS * 2 : rows;
  const last = Math.min(rows, first + visibleRows);
  const slice: React.ReactNode[] = [];

  if (itemW > 0) {
    for (let r = first; r < last; r += 1) {
      for (let c = 0; c < cols; c += 1) {
        const i = r * cols + c;
        if (i >= items.length) break;
        const v = items[i];
        slice.push(
          <div
            key={v.video_key || i}
            className="vgrid-cell"
            style={{
              transform: `translate3d(${PAD + c * (itemW + GAP)}px, ${r * rowH}px, 0)`,
              width: itemW,
              height: itemH,
            }}
          >
            <Card
              video={v}
              density={density}
              q={q}
              showText={withText}
              selected={selected?.has(v.video_key)}
              onSelectToggle={onSelectToggle}
            />
          </div>
        );
      }
    }
  }

  return (
    <div ref={scroller} className="vgrid" onScroll={onScroll}>
      <div className="vgrid-inner" style={{ height: totalH }}>
        {slice}
      </div>
      {footer && <div className="vgrid-footer">{footer}</div>}
    </div>
  );
}

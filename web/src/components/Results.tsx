/**
 * components/Results.tsx — the four view modes, in one place.
 *
 * Search and Library both show results, and in v1 they each had their own copy
 * of the grid. Copies drift: one gained the density slider and the other did
 * not, one fixed a paging bug and the other kept it. So the modes live here and
 * both views render `<Results>` — *literally the same component*, as the plan
 * puts it, "rather than a second copy that drifts".
 *
 * It also owns the four states a result set can be in, which is the other thing
 * eleven views should not each invent: first load (skeleton), reload with data
 * up (keep the old results, dim nothing), empty (say which of the three reasons
 * it is), and failed (say what the server said).
 */

import type { VideoItem } from '../types';
import type { GridMode } from '../lib/store';
import { fmtCount } from '../lib/format';
import Filmstrip from './Filmstrip';
import ResultRows from './ResultRows';
import VirtualGrid from './VirtualGrid';

export interface ResultsProps {
  items: VideoItem[];
  mode: GridMode;
  density: number;
  q?: string;
  total?: number;
  /** True only until the first successful response — the one time a skeleton is right. */
  first?: boolean;
  loading?: boolean;
  error?: string | null;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  emptyHead?: string;
  emptyNote?: string;
  selected?: Set<string>;
  onSelectToggle?: (key: string, e: React.MouseEvent) => void;
  laneView?: 'search' | 'library';
}

function Skeleton({ density }: { density: number }) {
  const n = Math.min(24, Math.max(6, density * 3));
  return (
    <div className="skel-grid" style={{ gridTemplateColumns: `repeat(${density}, 1fr)` }}>
      {Array.from({ length: n }, (_, i) => (
        <div className="skel skel-card" key={i} />
      ))}
    </div>
  );
}

export default function Results({
  items,
  mode,
  density,
  q,
  total,
  first,
  loading,
  error,
  onLoadMore,
  loadingMore,
  emptyHead = 'Nothing here',
  emptyNote,
  selected,
  onSelectToggle,
  laneView = 'search',
}: ResultsProps) {
  if (error) {
    return (
      <div className="state-box err">
        <div className="head">That request did not work</div>
        <div>{error}</div>
      </div>
    );
  }

  if (first && loading) return <Skeleton density={density} />;

  if (!items.length) {
    return (
      <div className="state-box">
        <div className="head">{emptyHead}</div>
        {emptyNote && <div>{emptyNote}</div>}
      </div>
    );
  }

  const footer =
    typeof total === 'number' && total > 0 ? (
      <span>
        {fmtCount(items.length)} of {fmtCount(total)}
        {loadingMore ? ' · loading more…' : items.length >= total ? ' · that is all of them' : ''}
      </span>
    ) : null;

  if (mode === 'list') {
    return (
      <ResultRows
        items={items}
        q={q}
        onLoadMore={onLoadMore}
        loadingMore={loadingMore}
        footer={footer}
      />
    );
  }

  if (mode === 'filmstrip') {
    return (
      <Filmstrip
        items={items}
        q={q}
        laneView={laneView}
        onLoadMore={onLoadMore}
        loadingMore={loadingMore}
        footer={footer}
      />
    );
  }

  // Contact sheet is the same grid with the footer off and a floor under the
  // column count — it is meant to be dense, and three columns of posters with
  // no text is just a grid with the labels missing.
  const contact = mode === 'contact';
  return (
    <VirtualGrid
      items={items}
      density={contact ? Math.max(density, 8) : density}
      q={q}
      showText={contact ? false : undefined}
      onLoadMore={onLoadMore}
      loadingMore={loadingMore}
      selected={selected}
      onSelectToggle={onSelectToggle}
      footer={footer}
    />
  );
}

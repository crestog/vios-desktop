/**
 * views/Library.tsx — everything in the archive, and the box that searches it.
 *
 * This view exists to fix a specific v1 bug, and the fix is the reason it does
 * not simply reuse Search: **the filter box re-queries the whole archive, not
 * the loaded page.** In v1 the library's box filtered the rows already in
 * memory, so typing a creator's name in a 200-row page of a 5,000-row archive
 * quietly searched 4% of it and reported "no matches". Here every keystroke goes
 * to `/api/library?q=`, which searches metadata *and* contents server-side and
 * returns `inside` so the view can say which half matched.
 *
 * The grid is `<Results>` — the same component Search renders, not a second
 * copy. That is deliberate: in v1 the two grids drifted until one had the
 * density slider and the other did not.
 *
 * **One documented deviation from the design doc.** It specifies "a detail rail
 * on hover". The rail here is driven by *selection* instead, because the hover
 * gesture is already spent on the clip preview, and at twelve columns a rail
 * that follows the pointer flickers through a dozen reels on the way to one.
 * Selection is explicit, survives a mouse leaving the grid, and is what the bulk
 * actions need anyway.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  GalleryHorizontal,
  Grid2x2,
  LayoutGrid,
  List,
  SlidersHorizontal,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { go } from '../lib/router';
import { LIBRARY_SORTS, getFacets, getLibrary } from '../lib/api';
import { store, useDensity, useGridMode, type GridMode } from '../lib/store';
import { useDebounced, useFetch } from '../lib/useFetch';
import { fmtCount, plural } from '../lib/format';
import { channelTally } from '../lib/channels';
import Results from '../components/Results';
import FacetRail from '../components/FacetRail';
import DetailRail from '../components/DetailRail';
import BulkBar from '../components/BulkBar';

const PAGE = 120;

/** What the data can actually filter on. Not a wish list — these three exist. */
const HAS: Array<{ id: string; label: string; note: string }> = [
  { id: '', label: 'everything', note: 'every reel on record' },
  { id: 'speech', label: 'has speech', note: 'a transcript with timings exists' },
  { id: 'narrative', label: 'described', note: 'a vision-language model has watched it' },
  { id: 'playable', label: 'playable', note: 'the file is on this disk or in the channel' },
];

const MODES: Array<{ id: GridMode; label: string; Icon: LucideIcon }> = [
  { id: 'contact', label: 'Contact sheet — dense, posters only', Icon: LayoutGrid },
  { id: 'grid', label: 'Grid — posters with titles', Icon: Grid2x2 },
  { id: 'list', label: 'Feed — one row each, full width', Icon: List },
  { id: 'filmstrip', label: 'By creator — one lane each', Icon: GalleryHorizontal },
];

export default function LibraryView({ route }: ViewProps) {
  const p = route.params;
  const urlQ = p.get('q') || '';
  const sort = p.get('sort') || 'recent';
  const creator = p.get('creator') || '';
  const category = p.get('category') || '';
  const has = p.get('has') || '';

  const density = useDensity();
  const mode = useGridMode();

  const [text, setText] = useState(urlQ);
  useEffect(() => setText(urlQ), [urlQ]);
  const typed = useDebounced(text, 160);

  useEffect(() => {
    if (typed === urlQ) return;
    go('library', { params: { q: typed, sort, creator, category, has }, replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed]);

  const [pages, setPages] = useState(1);
  useEffect(() => setPages(1), [urlQ, sort, creator, category, has]);
  const limit = PAGE * pages;

  const lib = useFetch(
    (signal) => getLibrary({ q: urlQ, limit, sort, creator, category, has }, signal),
    [urlQ, limit, sort, creator, category, has]
  );

  const facets = useFetch(getFacets, []);
  const channels = useMemo(() => channelTally(facets.data?.sources), [facets.data]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState<string | null>(null);

  // A new query invalidates a selection made against the old one — keeping it
  // would let a bulk requeue fire at reels no longer on screen.
  useEffect(() => {
    setSelected(new Set());
    setFocus(null);
  }, [urlQ, sort, creator, category, has]);

  const onSelectToggle = useCallback((key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    setFocus(key);
  }, []);

  const res = lib.data;
  const items = res?.results || [];

  const set = (patch: Record<string, unknown>) =>
    go('library', { params: { q: urlQ, sort, creator, category, has, ...patch } });

  const total = res?.total;
  const archiveTotal = facets.data?.totals?.videos;

  return (
    <div className="view view-split">
      <div className="view-bar">
        <input
          className="input-text search-box"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={
            archiveTotal
              ? `filter all ${plural(archiveTotal, 'reel')} — title, creator, caption or contents`
              : 'filter the whole archive — title, creator, caption or contents'
          }
          spellCheck={false}
          aria-label="Filter the library"
        />

        <div className="segmented" role="tablist" aria-label="View mode">
          {MODES.map(({ id, label, Icon }) => (
            <button
              key={id}
              className={mode === id ? 'on' : ''}
              onClick={() => store.setGridMode(id)}
              title={label}
              aria-pressed={mode === id}
            >
              <Icon size={12} />
            </button>
          ))}
        </div>

        {(mode === 'grid' || mode === 'contact') && (
          <label className="dens" title="columns — and which poster size is fetched">
            <SlidersHorizontal size={12} />
            <input
              type="range"
              min={3}
              max={12}
              step={1}
              value={density}
              onChange={(e) => store.setDensity(Number(e.target.value))}
            />
            <span className="dens-n">{density}</span>
          </label>
        )}

        <select
          className="input-text sel"
          value={has}
          onChange={(e) => set({ has: e.target.value })}
          aria-label="Which reels"
          title={HAS.find((h) => h.id === has)?.note}
        >
          {HAS.map((h) => (
            <option key={h.id || 'all'} value={h.id}>
              {h.label}
            </option>
          ))}
        </select>

        <select
          className="input-text sel"
          value={sort}
          onChange={(e) => set({ sort: e.target.value })}
          aria-label="Sort"
        >
          {LIBRARY_SORTS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        <span className="spacer" />

        <span className="view-sub" aria-live="polite">
          {res
            ? `${fmtCount(res.total)}${
                urlQ && typeof res.inside === 'number'
                  ? ` · ${fmtCount(res.inside)} matched inside the reel`
                  : ''
              }${lib.loading ? ' · …' : ''}`
            : ''}
        </span>
      </div>

      <div className="split">
        <FacetRail
          creators={facets.data?.creators}
          categories={facets.data?.categories}
          channels={channels}
          active={{ creator, category, source: '' }}
          onChange={(patch) => {
            // The library has no `source` filter server-side — a channel is a
            // property of the claims, not the reel. Send the channel to Search,
            // which does filter on it, rather than pretending it applies here.
            if ('source' in patch && patch.source) {
              go('search', { params: { q: urlQ || '*', source: String(patch.source) } });
              return;
            }
            set(patch);
          }}
        />

        <div className="split-main">
          <Results
            items={items}
            mode={mode}
            density={density}
            q={urlQ}
            total={total}
            first={lib.first}
            loading={lib.loading}
            error={lib.error}
            loadingMore={lib.loading}
            onLoadMore={() => {
              if (res && items.length < res.total) setPages((n) => n + 1);
            }}
            selected={selected}
            onSelectToggle={onSelectToggle}
            laneView="library"
            emptyHead={urlQ ? 'Nothing in the archive matches that' : 'The archive is empty'}
            emptyNote={
              res?.note ??
              (urlQ
                ? 'This searched every reel on record, not just the loaded page — so this is the whole answer.'
                : 'Nothing has been imported yet. Admin → restore the pinned bundle, or Capture → start pulling reels in.')
            }
          />

          {selected.size > 0 && (
            <BulkBar keys={[...selected]} onClear={() => setSelected(new Set())} />
          )}
        </div>

        {focus && (
          <DetailRail
            videoKey={focus}
            q={urlQ}
            onClose={() => setFocus(null)}
            hint={
              selected.size > 1
                ? `${fmtCount(selected.size)} selected — ctrl-click to add or remove`
                : 'ctrl-click a card to select more'
            }
          />
        )}
      </div>

      {!focus && items.length > 0 && (
        <div className="view-hint">ctrl-click a reel to open its details beside the grid</div>
      )}
    </div>
  );
}

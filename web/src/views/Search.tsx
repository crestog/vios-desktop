/**
 * views/Search.tsx — the half-memory to a moment, in under four seconds.
 *
 * Everything that narrows the search lives in the URL, and that is not a
 * stylistic preference: it means a search can be pasted into a note, reached
 * with Back, and reloaded after a restart. Only *how* the results are drawn —
 * density, view mode — lives in the store, because a column count has no
 * business travelling in a shared link.
 *
 * Two decisions worth reading before editing:
 *
 *   - **Paging grows the limit instead of walking offsets.** Asking for 120 and
 *     then 180 re-fetches rows already on screen, which sounds wasteful and is
 *     the right trade here: an offset walk over a *ranked* query duplicates and
 *     drops rows whenever two documents tie and sqlite breaks the tie
 *     differently between calls, and the fix for that is client-side dedupe
 *     nobody can verify. Against local sqlite an FTS5 query for 180 rows costs
 *     single-digit milliseconds, so correctness is simply cheaper.
 *
 *   - **The results stay on screen while the next query runs.** `useFetch`
 *     holds the previous data through a reload and exposes `loading` beside it;
 *     blanking to a skeleton on every keystroke makes a 90 ms search *feel*
 *     slower than a 400 ms one.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  GalleryHorizontal,
  Grid2x2,
  Image as ImageIcon,
  LayoutGrid,
  List,
  SlidersHorizontal,
  Type,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { go, num } from '../lib/router';
import { SEARCH_SORTS, getFacets, searchArchive, searchVisual } from '../lib/api';
import { useDensity, useGridMode, store, type GridMode } from '../lib/store';
import { useDebounced, useFetch } from '../lib/useFetch';
import { fmtCount, fmtMs, plural } from '../lib/format';
import { channelTally } from '../lib/channels';
import Results from '../components/Results';
import FacetRail from '../components/FacetRail';
import FrameHits from '../components/FrameHits';

const PAGE = 60;

const MODES: Array<{ id: GridMode; label: string; Icon: LucideIcon }> = [
  { id: 'contact', label: 'Contact sheet — dense, posters only', Icon: LayoutGrid },
  { id: 'grid', label: 'Grid — posters with titles', Icon: Grid2x2 },
  { id: 'list', label: 'List — with the matched passages', Icon: List },
  { id: 'filmstrip', label: 'By creator — one lane each', Icon: GalleryHorizontal },
];

export default function SearchView({ route }: ViewProps) {
  const p = route.params;
  const urlQ = p.get('q') || '';
  const sort = p.get('sort') || 'relevance';
  const creator = p.get('creator') || '';
  const category = p.get('category') || '';
  const source = p.get('source') || '';
  const lane = p.get('lane') === 'frames' ? 'frames' : 'text';
  const minDur = num(p, 'min_dur');
  const maxDur = num(p, 'max_dur');
  const minHits = num(p, 'min_hits');

  const density = useDensity();
  const mode = useGridMode();

  // The box is local so typing is never gated on a round trip; the URL is
  // updated with `replace` so Back returns to the previous *view* rather than
  // walking back through "h", "ho", "hoo".
  const [text, setText] = useState(urlQ);
  useEffect(() => setText(urlQ), [urlQ]);
  const typed = useDebounced(text, 140);

  useEffect(() => {
    if (typed === urlQ) return;
    go('search', {
      params: { q: typed, sort, creator, category, source, lane: lane === 'frames' ? 'frames' : '' },
      replace: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed]);

  const [pages, setPages] = useState(1);
  useEffect(() => setPages(1), [urlQ, sort, creator, category, source, minDur, maxDur, minHits]);
  const limit = PAGE * pages;

  const enabled = urlQ.trim().length > 0;

  const text_ = useFetch(
    (signal) =>
      searchArchive(
        {
          q: urlQ,
          limit,
          sort,
          creator,
          category,
          source,
          min_dur: minDur,
          max_dur: maxDur,
          min_hits: minHits,
        },
        signal
      ),
    [urlQ, limit, sort, creator, category, source, minDur, maxDur, minHits],
    { enabled: enabled && lane === 'text' }
  );

  const frames = useFetch(
    (signal) => searchVisual({ q: urlQ, limit: 120 }, signal),
    [urlQ],
    { enabled: enabled && lane === 'frames' }
  );

  // Channel counts for the source filter come from the archive as a whole, not
  // from the result set: a filter that only offered channels already present in
  // the current results could never be used to widen a search.
  const facets = useFetch(getFacets, []);
  const channels = useMemo(() => channelTally(facets.data?.sources), [facets.data]);

  const res = text_.data;
  const items = res?.results || [];

  const set = (patch: Record<string, unknown>) =>
    go('search', {
      params: {
        q: urlQ,
        sort,
        creator,
        category,
        source,
        min_dur: minDur,
        max_dur: maxDur,
        min_hits: minHits,
        lane: lane === 'frames' ? 'frames' : '',
        ...patch,
      },
    });

  return (
    <div className="view view-split">
      <div className="view-bar">
        <input
          className="input-text search-box"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="a line you half remember, a thing you saw, an idea…"
          autoFocus
          spellCheck={false}
          aria-label="Search"
        />

        <div className="segmented" role="tablist" aria-label="Search lane">
          <button
            className={lane === 'text' ? 'on' : ''}
            onClick={() => set({ lane: '' })}
            title="what was said, written and described"
          >
            <Type size={12} /> Words
          </button>
          <button
            className={lane === 'frames' ? 'on' : ''}
            onClick={() => set({ lane: 'frames' })}
            title="frames that look like the phrase — one result per frame, not per reel"
          >
            <ImageIcon size={12} /> Frames
          </button>
        </div>

        {lane === 'text' && (
          <>
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
              value={sort}
              onChange={(e) => set({ sort: e.target.value })}
              aria-label="Sort"
            >
              {SEARCH_SORTS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </>
        )}

        <span className="spacer" />

        <span className="view-sub" aria-live="polite">
          {lane === 'frames'
            ? frames.data
              ? `${plural(frames.data.count, 'frame')} · ${fmtMs(frames.data.took_ms)}`
              : ''
            : res
              ? `${fmtCount(res.total)}${
                  res.matched && res.matched !== res.total
                    ? ` of ${fmtCount(res.matched)} before filters`
                    : ''
                }${res.took_ms !== undefined ? ` · ${fmtMs(res.took_ms)}` : ''}${
                  text_.loading ? ' · …' : ''
                }`
              : ''}
        </span>
      </div>

      <div className="split">
        {lane === 'text' && (
          <FacetRail
            creators={res?.facets?.creators}
            categories={res?.facets?.categories}
            channels={channels}
            active={{ creator, category, source }}
            minDur={minDur}
            maxDur={maxDur}
            minHits={minHits}
            onChange={set}
          />
        )}

        <div className="split-main">
          {!enabled ? (
            <div className="state-box">
              <div className="head">Search the archive</div>
              <div>
                Every spoken line, every word on screen, every caption and every model's
                description — one box.
              </div>
            </div>
          ) : lane === 'frames' ? (
            <FrameHits
              hits={frames.data?.hits || []}
              reason={frames.data?.reason}
              error={frames.error}
              first={frames.first}
              loading={frames.loading}
              q={urlQ}
            />
          ) : (
            <Results
              items={items}
              mode={mode}
              density={density}
              q={urlQ}
              total={res?.total}
              first={text_.first}
              loading={text_.loading}
              error={text_.error}
              loadingMore={text_.loading}
              onLoadMore={() => {
                if (res && items.length < res.total) setPages((n) => n + 1);
              }}
              emptyHead="Nothing matched"
              emptyNote={
                res?.note ??
                (creator || category || source || minDur || maxDur || minHits
                  ? 'The query may have matched rows the filters then removed — try clearing one.'
                  : 'No spoken line, caption, on-screen text or model description in the archive contains that.')
              }
            />
          )}
        </div>
      </div>
    </div>
  );
}

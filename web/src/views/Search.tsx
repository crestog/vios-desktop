/**
 * views/Search.tsx — the half-memory to a moment, in under four seconds.
 *
 * Everything that narrows the search lives in the URL, and that is not a
 * stylistic preference: it means a search can be pasted into a note, reached
 * with Back, and reloaded after a restart. Only *how* the results are drawn —
 * density, view mode — lives in the store, because a column count has no
 * business travelling in a shared link.
 *
 * Three decisions worth reading before editing:
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
 *
 *   - **The Frames lane has three queries, and only one of them needs no model.**
 *     `?frame=<key>&ft=<seconds>` asks "more frames like this frame": the query
 *     vector is already in the database, so it is a cosine against a matrix and
 *     it answers with nothing installed. A typed phrase has to go through CLIP's
 *     text tower, which means torch. This lane used to send only the phrase, so
 *     on a machine without torch every frame search failed and the mode that
 *     needed nothing was unreachable — implemented in the server, implemented in
 *     the client, and called from nowhere.
 *
 *   - **The picture query is the one thing here that is not in the URL.** Reverse
 *     image search posts bytes to `/api/vsearch/image`, and a file cannot be a
 *     query parameter: it is not shareable, not reloadable, and it does not
 *     survive Back. That is a real cost against this view's one rule, and it is
 *     paid rather than worked around, because the alternative — writing the
 *     upload to a temp file server-side and putting a handle in the URL — makes
 *     the archive accumulate scratch files for a query that is usually asked
 *     once. The chip says the query dies on refresh instead of pretending.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  GalleryHorizontal,
  Grid2x2,
  Image as ImageIcon,
  LayoutGrid,
  List,
  ScanSearch,
  SlidersHorizontal,
  Type,
  X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { go, num } from '../lib/router';
import { SEARCH_SORTS, getFacets, searchArchive, searchImage, searchVisual } from '../lib/api';
import { useDensity, useGridMode, store, type GridMode } from '../lib/store';
import { useDebounced, useFetch } from '../lib/useFetch';
import { fmtCount, fmtMs, fmtT, plural } from '../lib/format';
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
  const collection = p.get('collection') || '';
  const source = p.get('source') || '';
  const lane = p.get('lane') === 'frames' ? 'frames' : 'text';
  const minDur = num(p, 'min_dur');
  const maxDur = num(p, 'max_dur');
  const minHits = num(p, 'min_hits');

  // The frame query. `frame` may be `<key>` or `<key>:<idx>`; `ft` is seconds,
  // which the server turns into a frame index because it holds `fps` and the
  // browser does not.
  const frameRef = p.get('frame') || '';
  const frameT = num(p, 'ft');
  const byFrame = lane === 'frames' && frameRef.length > 0;

  const density = useDensity();
  const mode = useGridMode();

  // ── the picture query ───────────────────────────────────────────────────
  // Session state, not URL state, because a File is not a query parameter —
  // see the note at the top of this file. `url` is an object URL for the chip's
  // thumbnail and has to be revoked by hand; a blob URL is a document-lifetime
  // reference and leaving them behind holds the whole image in memory.
  const [shot, setShot] = useState<{ blob: File | Blob; url: string; name: string } | null>(null);
  const shotRef = useRef<typeof shot>(null);
  shotRef.current = shot;
  useEffect(
    () => () => {
      if (shotRef.current) URL.revokeObjectURL(shotRef.current.url);
    },
    []
  );

  const takePicture = useCallback((blob: File | Blob | null | undefined) => {
    if (!blob) return;
    // A drop with a type that is plainly not an image is dropped on the floor —
    // but an *empty* type is let through, and that is the interesting case. A
    // file dragged from an unfamiliar extension arrives with `type: ''`, and
    // refusing it here means the interface does nothing at all and never says
    // why. Sent instead, the server answers `bad_image` with the decoder's own
    // complaint, and the lane already has a box for that. One wasted round trip
    // buys a real sentence over silence.
    const kind = blob.type || '';
    if (kind && !/^image\//.test(kind)) return;
    setShot((prev) => {
      if (prev) URL.revokeObjectURL(prev.url);
      return {
        blob,
        url: URL.createObjectURL(blob),
        name: (blob as File).name || 'pasted image',
      };
    });
  }, []);

  const clearPicture = useCallback(() => {
    setShot((prev) => {
      if (prev) URL.revokeObjectURL(prev.url);
      return null;
    });
  }, []);

  // Paste is the path that matters: a screenshot lives on the clipboard, and
  // Win+Shift+S then Ctrl+V is the whole interaction. Bound on the window so it
  // works without first focusing anything, and only in the frames lane so
  // pasting into the text box in the words lane still pastes text.
  useEffect(() => {
    if (lane !== 'frames') return;
    const onPaste = (e: ClipboardEvent) => {
      const items = Array.from(e.clipboardData?.items || []);
      const img = items.find((it) => it.kind === 'file' && /^image\//.test(it.type));
      if (!img) return;
      const blob = img.getAsFile();
      if (!blob) return;
      e.preventDefault();
      takePicture(blob);
    };
    window.addEventListener('paste', onPaste);
    return () => window.removeEventListener('paste', onPaste);
  }, [lane, takePicture]);

  // `dragging` paints the pane; `dragDepth` is why it is a counter and not the
  // boolean it looks like. `dragleave` fires every time the pointer crosses into
  // a child element — a tile, a caption — so a boolean flickers off over the
  // busiest part of the target. Counting enter/leave pairs is the depth, and only
  // depth zero is really outside.
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const byImage = lane === 'frames' && shot !== null;

  // A picture is a frames question, and it replaces the frame pivot rather than
  // stacking with it: two "look like this" queries at once has no meaning, and
  // silently ignoring one is worse than replacing it visibly. Done in an effect
  // keyed on `shot` rather than inside `takePicture` so it composes with `set`
  // below and cannot race the debounced box — clearing `q` here would navigate
  // to an empty query while `typed` still held the old text, and the box's own
  // effect would navigate straight back.
  useEffect(() => {
    if (!shot) return;
    if (lane !== 'frames' || frameRef) set({ lane: 'frames', frame: '', ft: '' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shot]);

  // The box is local so typing is never gated on a round trip; the URL is
  // updated with `replace` so Back returns to the previous *view* rather than
  // walking back through "h", "ho", "hoo".
  const [text, setText] = useState(urlQ);
  useEffect(() => setText(urlQ), [urlQ]);
  const typed = useDebounced(text, 140);

  useEffect(() => {
    if (typed === urlQ) return;
    go('search', {
      params: {
        q: typed,
        sort,
        creator,
        category,
        collection,
        source,
        lane: lane === 'frames' ? 'frames' : '',
        // Typing is a new question. A phrase and "like this frame" are different
        // queries, so one replaces the other rather than silently losing.
      },
      replace: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed]);

  const [pages, setPages] = useState(1);
  useEffect(
    () => setPages(1),
    [urlQ, sort, creator, category, collection, source, minDur, maxDur, minHits]
  );
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
          collection,
          source,
          min_dur: minDur,
          max_dur: maxDur,
          min_hits: minHits,
        },
        signal
      ),
    [urlQ, limit, sort, creator, category, collection, source, minDur, maxDur, minHits],
    { enabled: enabled && lane === 'text' }
  );

  const frames = useFetch(
    (signal) =>
      byImage
        ? searchImage(shot!.blob, 120, signal)
        : byFrame
          ? searchVisual({ frame: frameRef, t: frameT, limit: 120 }, signal)
          : searchVisual({ q: urlQ, limit: 120 }, signal),
    [byImage, shot?.blob, byFrame, frameRef, frameT, urlQ],
    { enabled: lane === 'frames' && (byImage || byFrame || enabled) }
  );

  // Channel counts for the source filter come from the archive as a whole, not
  // from the result set: a filter that only offered channels already present in
  // the current results could never be used to widen a search.
  const facets = useFetch(getFacets, []);
  const channels = useMemo(() => channelTally(facets.data?.sources), [facets.data]);

  const res = text_.data;
  const items = res?.results || [];

  // A frame query is a real query even with an empty box, so the placeholder
  // must not claim nothing has been asked. So is a picture.
  const asked = enabled || byFrame || byImage;

  const set = (patch: Record<string, unknown>) =>
    go('search', {
      params: {
        q: urlQ,
        sort,
        creator,
        category,
        collection,
        source,
        min_dur: minDur,
        max_dur: maxDur,
        min_hits: minHits,
        lane: lane === 'frames' ? 'frames' : '',
        frame: frameRef,
        ft: frameT,
        ...patch,
      },
    });

  return (
    <div className="view view-split">
      <div className="view-bar">
        <input
          className="input-text search-box"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            // A keystroke is a new question. Unambiguous here, where it is an
            // actual user action — inferring it from the debounced value would
            // also fire when the URL changed underneath.
            if (shot) clearPicture();
          }}
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
            title="one result per frame, not per reel — by phrase, or by a frame you point at"
          >
            <ImageIcon size={12} /> Frames
          </button>
        </div>

        {lane === 'frames' && (
          <>
            {/* Reverse image search. A label wrapping a hidden input rather than a
                button calling `.click()`: the native control is what makes drag
                onto the button work and what keeps it reachable from the
                keyboard. `key` on the input resets it, so choosing the same file
                twice in a row still fires a change event. */}
            <label
              className="q-chip pick-shot"
              title="find frames that look like a picture — paste a screenshot with Ctrl+V, drop one on the results, or pick a file"
            >
              <ScanSearch size={12} /> By picture
              <input
                key={shot?.url || 'empty'}
                type="file"
                accept="image/*"
                hidden
                onChange={(e) => takePicture(e.target.files?.[0])}
              />
            </label>

            {byImage && shot && (
              <button
                className="q-chip shot-chip"
                onClick={clearPicture}
                title={`searching by ${shot.name} — this query is not in the URL, so it will not survive a refresh`}
              >
                <img className="shot-thumb" src={shot.url} alt="" />
                like this picture <X size={11} />
              </button>
            )}
          </>
        )}

        {byFrame && (
          <button
            className="q-chip"
            onClick={() => set({ frame: '', ft: '' })}
            title="stop searching by that frame and go back to the phrase"
          >
            like {frameRef.split(':')[0]}
            {frameT ? ` @ ${fmtT(frameT)}` : ''} <X size={11} />
          </button>
        )}

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
              ? `${plural(frames.data.count, 'frame')}${
                  // How much of the archive was actually compared. Worth showing:
                  // the shortlist used to score nine reels of thirty and report
                  // nothing about the twenty-one it skipped.
                  frames.data.searched_videos
                    ? ` from ${plural(frames.data.searched_videos, 'reel')} searched`
                    : ''
                } · ${fmtMs(frames.data.took_ms)}${frames.loading ? ' · …' : ''}`
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
            collections={res?.facets?.collections}
            channels={channels}
            active={{ creator, category, source, collection }}
            minDur={minDur}
            maxDur={maxDur}
            minHits={minHits}
            onChange={set}
          />
        )}

        <div
          className={`split-main${dragging ? ' dropping' : ''}`}
          // Drop is bound on the results pane rather than the whole window: a
          // drag ending on the sidebar or the toolbar is more likely a misfire
          // than a query, and `dragover` must be cancelled or the browser
          // navigates away to the file — which in the desktop shell means the
          // interface is simply gone and there is no address bar to come back
          // from. `dragleave` fires when crossing into a child, so the counter
          // is the depth rather than a boolean.
          //
          // Unlike paste, this is live in *both* lanes, and that asymmetry is
          // deliberate: dropping an image file says only one thing, so the
          // effect on `shot` moves the lane for you. A paste in the words lane
          // is usually a phrase, and hijacking it would be a guess.
          onDragEnter={(e) => {
            if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return;
            e.preventDefault();
            dragDepth.current += 1;
            setDragging(true);
          }}
          onDragOver={(e) => {
            if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return;
            e.preventDefault();
          }}
          onDragLeave={() => {
            dragDepth.current = Math.max(0, dragDepth.current - 1);
            if (!dragDepth.current) setDragging(false);
          }}
          onDrop={(e) => {
            if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return;
            e.preventDefault();
            dragDepth.current = 0;
            setDragging(false);
            takePicture(e.dataTransfer?.files?.[0]);
          }}
        >
          {!asked ? (
            <div className="state-box">
              <div className="head">Search the archive</div>
              <div>
                Every spoken line, every word on screen, every caption and every model's
                description — one box. Or switch to <em>Frames</em> and hand it a picture:
                paste a screenshot, drop an image here, or pick a file.
              </div>
            </div>
          ) : lane === 'frames' ? (
            <FrameHits
              hits={frames.data?.hits || []}
              reason={frames.data?.reason}
              cause={frames.data?.cause}
              error={frames.error}
              first={frames.first}
              loading={frames.loading}
              q={urlQ}
              byFrame={byFrame}
              byImage={byImage}
              onPivot={(key, t) => {
                clearPicture();
                set({ frame: key, ft: t.toFixed(2) });
              }}
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
                (creator || category || collection || source || minDur || maxDur || minHits
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

/**
 * lib/api.ts — the one place that knows a URL.
 *
 * Three things this file is responsible for, and nothing else:
 *
 *   1. **One fetch path.** Every request goes through `request()`, so there is
 *      one place that adds the base, one that parses an error, one that
 *      handles abort. A view that wants a different timeout does not get one.
 *
 *   2. **Abort as a first-class case.** Search fires on every keystroke, so
 *      the previous request is *cancelled*, not awaited — that is the whole
 *      difference between a 120 ms search box and one that queues five
 *      requests and paints the third. An aborted request throws `Aborted`,
 *      which views swallow: it is not an error, it is the newer keystroke
 *      doing its job.
 *
 *   3. **URL shapes that match the server.** These are transcribed from the
 *      route decorators in `atlas/server.py`, which uses path parameters for
 *      keys (`/api/video/{key}`, not `?key=`) and returns `results` rather
 *      than `items`. Guessing here produces a 404 or, worse, a 200 with an
 *      empty list that reads as "no data".
 */

import type {
  ArchiveStatus,
  CellProvenance,
  ComponentCatalogue,
  DerivedState,
  DiskUsage,
  EngineJob,
  EngineStats,
  Facet,
  FacetsResponse,
  GraphEdgeDetail,
  GraphNode,
  GraphNodeDetail,
  GraphPathResponse,
  GraphResponse,
  HostFacts,
  KeyframeIndex,
  LibraryResponse,
  LocalVideo,
  MirrorStatus,
  RoadmapGoal,
  RoadmapMarkResponse,
  RoadmapResponse,
  RoadmapStepDetail,
  SchemaResponse,
  SearchResponse,
  SpriteMeta,
  TableResponse,
  VideoDetail,
  WatchedFolder,
} from '../types';

const API = '/api';

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public url: string
  ) {
    super(`${status} ${detail}`);
    this.name = 'ApiError';
  }
}

/** Thrown when a newer request superseded this one. Not a failure. */
export class Aborted extends Error {
  constructor() {
    super('aborted');
    this.name = 'Aborted';
  }
}

export function isAborted(e: unknown): boolean {
  return (
    e instanceof Aborted ||
    (e instanceof DOMException && e.name === 'AbortError') ||
    (e as { name?: string })?.name === 'AbortError'
  );
}

async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const url = path.startsWith('/api') ? path : `${API}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      signal,
      headers:
        init.body !== undefined
          ? { 'Content-Type': 'application/json', ...(init.headers || {}) }
          : init.headers,
    });
  } catch (e) {
    if (isAborted(e)) throw new Aborted();
    // A network-level failure against localhost means the Python side died or
    // is still booting. Said plainly, because "Failed to fetch" is not a
    // diagnosis anyone can act on.
    throw new ApiError(0, 'the local server is not answering', url);
  }

  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body?.detail || body?.note || body?.message || detail;
    } catch {
      /* a non-JSON error body is normal for a 500 traceback page */
    }
    throw new ApiError(res.status, String(detail), url);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// `object` rather than `Record<string, unknown>`: an interface like `SearchArgs`
// has no index signature, so the stricter type forces every caller into a cast
// that is exactly what a cast should not be — noise around a safe operation.
function qs(params: object): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    // `0` and `false` are real values and must survive; `''`/null/undefined
    // are "not set" and are dropped so the server applies its own default.
    if (v === undefined || v === null || v === '') continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

// ── Archive state ─────────────────────────────────────────────────────────
export const getStatus = (signal?: AbortSignal) =>
  request<ArchiveStatus>('/status', {}, signal);

export const getFacets = (signal?: AbortSignal) =>
  request<FacetsResponse>('/facets', {}, signal);

export const getBundles = (signal?: AbortSignal) =>
  request<{ ok: boolean; bundles: Array<Record<string, unknown>> }>('/bundles', {}, signal);

export const getChannelInfo = (signal?: AbortSignal) =>
  request<Record<string, unknown>>('/channel', {}, signal);

export const getLog = (limit = 120, signal?: AbortSignal) =>
  request<{ lines: string[] } | Record<string, unknown>>(`/log${qs({ limit })}`, {}, signal);

// ── Search ────────────────────────────────────────────────────────────────
/** Valid values for `sort` on `/api/search`, in the order the UI offers them. */
export const SEARCH_SORTS = [
  'relevance',
  'matches',
  'recent',
  'oldest',
  'longest',
  'shortest',
  'liked',
] as const;

/** `/api/library` sorts. A different set, because it sorts rows not matches. */
export const LIBRARY_SORTS = [
  'recent',
  'oldest',
  'richest',
  'longest',
  'shortest',
  'liked',
] as const;

export interface SearchArgs {
  q: string;
  limit?: number;
  offset?: number;
  sort?: string;
  creator?: string;
  category?: string;
  source?: string;
  video?: string;
  min_dur?: number;
  max_dur?: number;
  min_hits?: number;
}

export const searchArchive = (args: SearchArgs, signal?: AbortSignal) =>
  request<SearchResponse>(`/search${qs(args)}`, {}, signal);

/**
 * The image lane. A **different unit of result**: frames, not videos.
 *
 * Deliberately not folded into `searchArchive` behind a mode flag, because the
 * two cannot be merged in the UI either — `/api/search` returns videos with
 * matched passages, `/api/vsearch` returns `{video_key, frame_idx, t, score}`
 * with one row per *frame*, several of which are usually the same video. A
 * shared type would force one of them to lie about what it found.
 *
 * `t` is null when the video has no frame rate on record: the frame index is
 * exact and the seconds are derived, so the server declines to guess 30 fps.
 */
export interface VisualHit {
  video_key: string;
  frame_idx: number;
  t: number | null;
  score: number;
  space: string;
}

export interface VisualSearchResponse {
  ok: boolean;
  count: number;
  took_ms: number;
  hits: VisualHit[];
  space: string;
  query?: Record<string, unknown>;
  searched_videos?: number;
  /** Why the list is empty — four different causes look identical otherwise. */
  reason?: string;
}

export const searchVisual = (
  args: { q?: string; frame?: string; t?: number; space?: string; limit?: number; same_video?: boolean },
  signal?: AbortSignal
) => request<VisualSearchResponse>(`/vsearch${qs(args as Record<string, unknown>)}`, {}, signal);

export const suggest = (q: string, limit = 8, signal?: AbortSignal) =>
  request<{ suggestions: string[] }>(`/suggest${qs({ q, limit })}`, {}, signal);

export const similarTo = (key: string, limit = 12, signal?: AbortSignal) =>
  request<{ results: Array<Record<string, unknown>> }>(
    `/similar/${encodeURIComponent(key)}${qs({ limit })}`,
    {},
    signal
  );

// ── Library ───────────────────────────────────────────────────────────────
export interface LibraryArgs {
  limit?: number;
  offset?: number;
  sort?: string;
  creator?: string;
  category?: string;
  /** 'speech' | 'narrative' | 'playable' — filters the data actually supports. */
  has?: string;
  q?: string;
}

export const getLibrary = (args: LibraryArgs, signal?: AbortSignal) =>
  request<LibraryResponse>(`/library${qs(args as Record<string, unknown>)}`, {}, signal);

// ── One video ─────────────────────────────────────────────────────────────
export const getVideo = (key: string, full = true, signal?: AbortSignal) =>
  request<VideoDetail>(`/video/${encodeURIComponent(key)}${qs({ full })}`, {}, signal);

export const getMediaState = (key: string, signal?: AbortSignal) =>
  request<Record<string, unknown>>(`/media/${encodeURIComponent(key)}/state`, {}, signal);

export const getClips = (key: string, t0?: number, t1?: number, signal?: AbortSignal) =>
  request<Record<string, unknown>>(
    `/clips/${encodeURIComponent(key)}${qs({ t0, t1 })}`,
    {},
    signal
  );

export const prefetch = (keys: string[]) =>
  request<Record<string, unknown>>(`/prefetch${qs({ keys: keys.join(',') })}`, { method: 'POST' });

// ── Media URLs ────────────────────────────────────────────────────────────
// Not fetched through `request()` — these go straight into `src` attributes,
// where the browser's own range-request and cache machinery is what makes
// playback fast. Wrapping them in fetch would defeat both.

/** The playable stream. Range requests are served, so seeking works. */
export const playUrl = (key: string) => `${API}/play/${encodeURIComponent(key)}`;

/**
 * A poster at a named tier — 160 for a 12-column grid, 720 for a 3-column one.
 *
 * This is the density slider's real mechanism. Changing only the CSS width
 * would still fetch 720-wide JPEGs for a twelve-across contact sheet, which
 * is ~20× the bytes for pixels no one can see.
 */
export const posterUrl = (key: string, tier: 160 | 360 | 720 = 360) =>
  `${API}/derived/poster/${encodeURIComponent(key)}${qs({ tier })}`;

/** Atlas's on-demand extractor: any timestamp, one frame, no derive needed. */
export const framePosterUrl = (key: string, t?: number) =>
  `${API}/poster/${encodeURIComponent(key)}${qs({ t })}`;

/** A pre-cut ~4 s loop starting at `t`. What a card plays on hover. */
export const clipUrl = (key: string, t: number) =>
  `${API}/clip/${encodeURIComponent(key)}${qs({ t })}`;

/** One extracted frame by index, or the frame nearest a timestamp. */
export const frameUrl = (key: string, opts: { i?: number; t?: number }) =>
  `${API}/frame/${encodeURIComponent(key)}${qs(opts)}`;

export const spriteUrl = (key: string) => `${API}/derived/sprite/${encodeURIComponent(key)}`;

export const keyframeUrl = (key: string, file: string) =>
  `${API}/derived/keyframes/${encodeURIComponent(key)}/${encodeURIComponent(file)}`;

export const getSpriteMeta = (key: string, signal?: AbortSignal) =>
  request<SpriteMeta>(`/derived/sprite/${encodeURIComponent(key)}/meta`, {}, signal);

export const getKeyframes = (key: string, signal?: AbortSignal) =>
  request<KeyframeIndex>(`/derived/keyframes/${encodeURIComponent(key)}`, {}, signal);

export const getDerivedState = (key: string, signal?: AbortSignal) =>
  request<DerivedState>(`/derived/state/${encodeURIComponent(key)}`, {}, signal);

// ── The raw database ──────────────────────────────────────────────────────
export const getSchema = (samples = 0, signal?: AbortSignal) =>
  request<SchemaResponse>(`/schema${qs({ samples })}`, {}, signal);

export const getTable = (
  name: string,
  args: { limit?: number; offset?: number; q?: string; order?: string; desc?: boolean } = {},
  signal?: AbortSignal
) =>
  request<TableResponse>(
    `/table/${encodeURIComponent(name)}${qs(args as Record<string, unknown>)}`,
    {},
    signal
  );

/**
 * The provenance panel. `rowid` when we have one, `value` when we do not.
 *
 * A WITHOUT ROWID table has no rowid, and then the server explains the column
 * and the value rather than the row — which is why both are optional here
 * instead of `rowid` being required.
 */
export const getCell = (
  args: { table: string; column: string; rowid?: number; value?: string },
  signal?: AbortSignal
) => request<CellProvenance>(`/cell${qs(args as Record<string, unknown>)}`, {}, signal);

// ── Graph ─────────────────────────────────────────────────────────────────
export const getGraph = (limit = 16, signal?: AbortSignal) =>
  request<GraphResponse>(`/graph${qs({ limit })}`, {}, signal);

export const expandNode = (id: string, limit = 0, kind = '', signal?: AbortSignal) =>
  request<GraphResponse>(
    `/graph/expand/${encodeURI(id)}${qs({ limit, kind })}`,
    {},
    signal
  );

export const getNode = (id: string, rows = 40, signal?: AbortSignal) =>
  request<GraphNodeDetail>(`/graph/node/${encodeURI(id)}${qs({ rows })}`, {}, signal);

export const getEdge = (
  src: string,
  dst: string,
  rel: string,
  rows = 20,
  signal?: AbortSignal
) => request<GraphEdgeDetail>(`/graph/edge${qs({ src, dst, rel, rows })}`, {}, signal);

// `results`, not `nodes` — `/api/graph/find` wraps `graph.find()` in the same
// `{ok, results}` envelope every other list endpoint uses.
export const findNodes = (q: string, limit = 30, signal?: AbortSignal) =>
  request<{ ok: boolean; results: GraphNode[] }>(
    `/graph/find${qs({ q, limit })}`,
    {},
    signal
  );

export const findPath = (a: string, b: string, depth = 6, signal?: AbortSignal) =>
  request<GraphPathResponse>(`/graph/path${qs({ a, b, depth })}`, {}, signal);

export const getGraphSchema = (signal?: AbortSignal) =>
  request<Record<string, unknown>>('/graph/schema', {}, signal);

export const graphFromVideos = (keys: string[], limit = 24, signal?: AbortSignal) =>
  request<GraphResponse>(`/graph/from${qs({ keys: keys.join(','), limit })}`, {}, signal);

export const rebuildGraph = () =>
  request<Record<string, unknown>>('/graph/rebuild', { method: 'POST' });

// ── Roadmap ───────────────────────────────────────────────────────────────
/**
 * `breadth` and `min_support` are the two knobs that change *what is planned*
 * rather than how it looks, so they belong in a link. The server clamps them
 * (6–200 and 1–50) and treats 0 as "your default", which is why they are
 * plain numbers here with no client-side clamp duplicating that.
 */
export const getRoadmap = (goal = '', breadth = 0, min_support = 0, signal?: AbortSignal) =>
  request<RoadmapResponse>(`/roadmap${qs({ goal, breadth, min_support })}`, {}, signal);

export const getRoadmapStep = (id: string, goal = '', limit = 0, signal?: AbortSignal) =>
  request<RoadmapStepDetail>(`/roadmap/step/${encodeURI(id)}${qs({ goal, limit })}`, {}, signal);

export const getRoadmapGoals = (limit = 14, signal?: AbortSignal) =>
  request<{ ok: boolean; goals: RoadmapGoal[] }>(`/roadmap/goals${qs({ limit })}`, {}, signal);

export const getRoadmapProgress = (signal?: AbortSignal) =>
  request<{
    ok: boolean;
    progress: Record<string, { state: string; goal: string; at: number }>;
    counts: Record<string, number>;
  }>('/roadmap/progress', {}, signal);

/**
 * `state` is `done`, `skip`, or `''` to clear this one step.
 *
 * The empty string reaching the server as an absent parameter is not an
 * accident: `qs` drops it, FastAPI applies its `state: str = ""` default, and
 * `roadmap.mark()` treats an empty state as a delete. Same outcome, one path.
 */
export const markRoadmapStep = (step_id: string, state: string, goal = '') =>
  request<RoadmapMarkResponse>(`/roadmap/progress${qs({ step_id, state, goal })}`, {
    method: 'POST',
  });

/** Forget every tick. The plan is derived, so this loses nothing else. */
export const clearRoadmapProgress = () =>
  request<RoadmapMarkResponse>(`/roadmap/progress${qs({ clear: true })}`, { method: 'POST' });

// ── Scan & index ──────────────────────────────────────────────────────────
export const startScan = (full = true, max_messages = 0) =>
  request<Record<string, unknown>>(`/scan${qs({ full, max_messages })}`, { method: 'POST' });

export const reindex = (embed = true) =>
  request<Record<string, unknown>>(`/reindex${qs({ embed })}`, { method: 'POST' });

export const getVsearchState = (signal?: AbortSignal) =>
  request<Record<string, unknown>>('/vsearch/state', {}, signal);

export const buildVsearch = () =>
  request<Record<string, unknown>>('/vsearch/build', { method: 'POST' });

// ── Mirror worker ─────────────────────────────────────────────────────────
export const getMirrorStatus = (signal?: AbortSignal) =>
  request<MirrorStatus>('/mirror/status', {}, signal);

export const startMirror = () =>
  request<{ ok: boolean; status: MirrorStatus }>('/mirror/start', { method: 'POST' });

export const pauseMirror = () =>
  request<{ ok: boolean; status: MirrorStatus }>('/mirror/pause', { method: 'POST' });

export const resumeMirror = () =>
  request<{ ok: boolean; status: MirrorStatus }>('/mirror/resume', { method: 'POST' });

/** Jump one video to the front of the mirror queue — "I want to watch this now". */
export const prioritizeMirror = (key: string) =>
  request<{ ok: boolean; key: string }>(`/mirror/prioritize/${encodeURIComponent(key)}`, {
    method: 'POST',
  });

// ── Local library ─────────────────────────────────────────────────────────
export const getWatchedFolders = (signal?: AbortSignal) =>
  request<WatchedFolder[]>('/library/folders', {}, signal);

export const addWatchedFolder = (path: string) =>
  request<Record<string, unknown>>('/library/folders', {
    method: 'POST',
    body: JSON.stringify({ path }),
  });

export const removeWatchedFolder = (id: number) =>
  request<{ ok: boolean }>(`/library/folders/${id}`, { method: 'DELETE' });

export const scanWatchedFolders = () =>
  request<Record<string, unknown>>('/library/scan', { method: 'POST' });

export const getLocalVideos = (
  args: { status?: string; limit?: number; offset?: number } = {},
  signal?: AbortSignal
) => request<LocalVideo[]>(`/library/local${qs(args as Record<string, unknown>)}`, {}, signal);

// ── Engine queue ──────────────────────────────────────────────────────────
export const getEngineStats = (signal?: AbortSignal) =>
  request<EngineStats>('/engine/stats', {}, signal);

export const getEngineJobs = (state?: string, limit = 100, signal?: AbortSignal) =>
  request<EngineJob[]>(`/engine/jobs${qs({ state, limit })}`, {}, signal);

export const enqueueVideo = (video_key: string, component_ids?: string[]) =>
  request<{ ok: boolean; enqueued: number }>('/engine/enqueue', {
    method: 'POST',
    body: JSON.stringify({ video_key, component_ids: component_ids ?? null }),
  });

// The worker is a daemon thread, so these flip a flag and hand back the fresh
// stats — the button reflects its own effect without waiting for the next poll.
export const startEngine = () =>
  request<{ ok: boolean; stats: EngineStats }>('/engine/start', { method: 'POST' });

export const pauseEngine = () =>
  request<{ ok: boolean; stats: EngineStats }>('/engine/pause', { method: 'POST' });

export const resumeEngine = () =>
  request<{ ok: boolean; stats: EngineStats }>('/engine/resume', { method: 'POST' });

// The pipeline catalogue: what every component_id means and which passes this
// machine can host. Static but for the runnability flags, so a view reads it
// once per mount. `refresh` re-probes the GPU first (free VRAM has moved).
export const getComponents = (refresh = false, signal?: AbortSignal) =>
  request<ComponentCatalogue>(`/engine/components${qs({ refresh })}`, {}, signal);

// ── Machine ───────────────────────────────────────────────────────────────
export const getDisk = (signal?: AbortSignal) =>
  request<DiskUsage>('/desktop/disk', {}, signal);

export const getHost = (refresh = false, signal?: AbortSignal) =>
  request<HostFacts>(`/desktop/host${qs({ refresh })}`, {}, signal);

// ── The native shell ──────────────────────────────────────────────────────
declare global {
  interface Window {
    pywebview?: {
      api: {
        pick_folder?: () => Promise<string>;
        open_home?: () => Promise<string>;
        open_path?: (p: string) => Promise<string>;
      };
    };
  }
}

/** True when running inside the desktop window rather than a browser tab. */
export const isNative = () => Boolean(window.pywebview?.api);

/**
 * The native folder dialog — the concrete reason this app is pywebview and
 * not a browser tab. A tab can be handed files; it cannot be handed a *path*,
 * and indexing in place needs the path.
 *
 * Under `npm run dev` there is no bridge, so it falls back to a prompt. That
 * is a development affordance, not the product: the returned string is passed
 * to the server, which validates that the directory exists.
 */
export async function pickFolder(): Promise<string> {
  const bridge = window.pywebview?.api?.pick_folder;
  if (bridge) {
    try {
      return (await bridge()) || '';
    } catch (e) {
      console.warn('native folder dialog failed', e);
      return '';
    }
  }
  return window.prompt('Absolute path of the folder to watch:')?.trim() || '';
}

export async function openHome(): Promise<void> {
  try {
    await window.pywebview?.api?.open_home?.();
  } catch {
    /* nothing to do in a browser tab */
  }
}

export async function openPath(p: string): Promise<void> {
  try {
    await window.pywebview?.api?.open_path?.(p);
  } catch {
    /* nothing to do in a browser tab */
  }
}

export type { Facet };

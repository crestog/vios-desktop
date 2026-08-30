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
  BackfillStatus,
  BundlesResponse,
  CaptureCollection,
  CaptureCounts,
  CaptureEvent,
  CaptureFailure,
  CaptureQueueResponse,
  CaptureSettings,
  CaptureStatus,
  CaptureTask,
  CellProvenance,
  ComponentCatalogue,
  CredentialFields,
  CredentialForgetResult,
  CredentialSaveResult,
  DeconstructResponse,
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
  PatternsResponse,
  PreflightResponse,
  RestoreStarted,
  RestoreStatus,
  RoadmapGoal,
  RoadmapMarkResponse,
  RoadmapResponse,
  RoadmapStepDetail,
  SchemaResponse,
  ScriptResponse,
  SearchResponse,
  SpriteMeta,
  StatusEnvelope,
  StoredCredentials,
  TableResponse,
  VideoDetail,
  WatchedFolder,
  WireReport,
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
  // FormData must be sent *without* a Content-Type: the browser generates one
  // with the multipart boundary, and stating `application/json` over it makes
  // FastAPI's `Form(...)` parameters arrive empty. Every `/api/capture/*` write
  // takes a form, so this is not a special case — it is half the POSTs.
  const isForm = init.body instanceof FormData;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      signal,
      headers:
        init.body !== undefined && !isForm
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
      // Four keys because three subsystems disagree: FastAPI raises `detail`,
      // atlas returns `note`, `capture/routes.py:_err` returns `error`. Reading
      // only the first two turns capture's careful "Save the bot token and
      // channel id first." into a bare "Bad Request".
      detail = body?.detail || body?.note || body?.error || body?.message || detail;
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
// The whole envelope, not `envelope.search`: the counts are one level down, and
// a helper that quietly returned the inner block would make `/api/status`'s
// other five blocks unreachable through the only function that fetches it.
export const getStatus = (signal?: AbortSignal) =>
  request<StatusEnvelope>('/status', {}, signal);

export const getFacets = (signal?: AbortSignal) =>
  request<FacetsResponse>('/facets', {}, signal);

/**
 * File reels under a saved collection. One call for the whole selection,
 * unlike `enqueueVideo` — this is a single statement about a set, and the server
 * writes it in one transaction rather than N.
 *
 * Additive by contract: it never removes a label. `unknown` names any key the
 * archive did not recognise, which is reported rather than silently dropped.
 */
export const addToCollection = (collection: string, keys: string[]) =>
  request<{
    ok: boolean;
    collection: string;
    videos: number;
    added: number;
    unknown: string[];
  }>(`/collections/add${qs({ collection, keys: keys.join(',') })}`, { method: 'POST' });

export const getBundles = (signal?: AbortSignal) =>
  request<BundlesResponse>('/bundles', {}, signal);

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
  /** One saved collection. Narrows the videos, never the moments inside them. */
  collection?: string;
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
  /** One saved collection, asked of the membership table server-side. */
  collection?: string;
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

/**
 * Jump one video to the front of the mirror queue — "I want to watch this now".
 *
 * `state` is the mirror's real verdict: `queued` (it is now next in line),
 * `downloading` / `deriving` (already in flight), `ready` (nothing to do, the
 * file is complete on this disk) or `unknown` (the mirror has never heard of
 * this reel — it is in neither the search index nor the channel's upload
 * ledger). `note` is a sentence fit to show. Callers must show it rather than
 * assuming success: this endpoint used to answer `{ ok: true }` unconditionally,
 * so "Download now" reported that it had queued reels it had silently dropped.
 */
export type PrioritizeResult = {
  ok: boolean;
  key: string;
  state: 'queued' | 'downloading' | 'deriving' | 'ready' | 'unknown' | 'invalid';
  note: string;
  position?: number;
  queue_depth?: number;
  percent?: number;
};

export const prioritizeMirror = (key: string) =>
  request<PrioritizeResult>(`/mirror/prioritize/${encodeURIComponent(key)}`, {
    method: 'POST',
  });

/** Which reels the mirror has not finished, and why. */
export const getMirrorBacklog = (signal?: AbortSignal) =>
  request<{
    ok: boolean;
    waiting: number;
    items: {
      key: string;
      msg_id: number | null;
      expected_bytes: number;
      have_original: boolean;
      why: string;
      attempts: number;
      last_error: string;
      indexed: boolean;
    }[];
  }>('/mirror/backlog', {}, signal);

/** Re-measure every local original against the byte count Telegram declared. */
export const verifyMirror = () =>
  request<{
    ok: boolean;
    total: number;
    verified: number;
    unverified: number;
    incomplete: number;
    missing: number;
    note: string;
  }>('/mirror/verify', { method: 'POST' });

/** Retire the Telegram session and open a fresh one. */
export const reconnectMirror = () =>
  request<{ ok: boolean; transport: Record<string, unknown> }>('/mirror/reconnect', {
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

/**
 * Queue passes on one reel.
 *
 * `enqueued` on its own cannot be read: zero means "already processed" as often
 * as "nothing could run here", and the button that calls this used to say
 * *"queued 0 passes"* for both. So the answer is three numbers — what was
 * queued, what needed nothing done, and what this machine cannot host, each with
 * its reason.
 *
 * `force` re-runs passes that already completed. Off by default: the normal
 * meaning of processing a reel is "do what is missing", and a sweep that redid
 * finished work would never reach the end of a library.
 */
export const enqueueVideo = (
  video_key: string,
  component_ids?: string[],
  force = false
) =>
  request<{
    ok: boolean;
    enqueued: number;
    already: number;
    blocked: Record<string, string>;
    order: string[];
  }>('/engine/enqueue', {
    method: 'POST',
    body: JSON.stringify({ video_key, component_ids: component_ids ?? null, force }),
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

// ── Studio ────────────────────────────────────────────────────────────────
// Three reads over the same tables Search reads, all derived on request and
// cached server-side against a fingerprint of those tables. There is nothing to
// start, nothing to poll and nothing to write, which is why every one of these
// takes a signal and none of them is a POST.

/**
 * One reel taken apart: pacing, channels, sections, hook, gaps, claims.
 *
 * 404s for a key that is not indexed — `/studio?key=…` is a real address that
 * can be typed or bookmarked, so "not in the index" has to be distinguishable
 * from "indexed but empty", which is a 200 with `notes` explaining why.
 */
export const getDeconstruct = (key: string, signal?: AbortSignal) =>
  request<DeconstructResponse>(`/studio/deconstruct/${encodeURIComponent(key)}`, {}, signal);

/**
 * Distributions across a scope.
 *
 * A scope is a goal (one hybrid search), a creator, a category, or nothing at
 * all — and a goal matching fewer than three reels silently falls back to the
 * whole archive, which the response says in `scope.note` rather than in a
 * status code. Empty archive is a 200 with `reels: 0`.
 */
export const getPatterns = (
  args: { goal?: string; creator?: string; category?: string } = {},
  signal?: AbortSignal
) => request<PatternsResponse>(`/studio/patterns${qs(args)}`, {}, signal);

/** The same scope as a beat sheet: medians, ranked phrases, cited examples. */
export const getScriptDraft = (
  args: { goal?: string; creator?: string; category?: string } = {},
  signal?: AbortSignal
) => request<ScriptResponse>(`/studio/script${qs(args)}`, {}, signal);

// ── Admin ─────────────────────────────────────────────────────────────────
// JSON, unlike capture's writes: `server/admin_routes.py` declares a pydantic
// model rather than `Form(...)` fields, because there is no file upload here
// and a credential is better typed once than spread across six form parts.

/** Presence and origin for all six secrets. Never a value. */
export const getCredentials = (signal?: AbortSignal) =>
  request<StoredCredentials>('/admin/credentials', {}, signal);

/**
 * Store credentials, and make them live in the same call.
 *
 * Blank fields must be **dropped before calling this**, not sent as `''`: the
 * server treats an absent field as "leave that credential alone", which is what
 * lets a form that starts empty change one secret without deleting five.
 */
export const saveCredentials = (fields: CredentialFields) =>
  request<CredentialSaveResult>('/admin/credentials', {
    method: 'POST',
    body: JSON.stringify(fields),
  });

/** Delete the stored file. Does not unset this process's environment. */
export const forgetCredentials = () =>
  request<CredentialForgetResult>('/admin/credentials/forget', { method: 'POST' });

/** Schema numbers on both sides of the channel, and the verdict. */
export const getWire = (signal?: AbortSignal) =>
  request<WireReport>('/admin/wire', {}, signal);

export const getRestore = (signal?: AbortSignal) =>
  request<RestoreStatus>('/admin/restore', {}, signal);

/** Read the newest manifest and compute a plan. Writes nothing. */
export const inspectRestore = () =>
  request<RestoreStarted>('/admin/restore/inspect', { method: 'POST' });

/**
 * Destructive. `confirm` is always true here — the guard is that this function
 * is only reachable from a button the panel renders after a plan is on screen.
 * `seq` pins the bundle the user was actually looking at, so a panel left open
 * for ten minutes does not apply whatever the channel holds now.
 */
export const applyRestore = (seq?: string | null) =>
  request<RestoreStarted>('/admin/restore/apply', {
    method: 'POST',
    body: JSON.stringify({ confirm: true, seq: seq ?? null }),
  });

// ── Capture ───────────────────────────────────────────────────────────────
/**
 * Every write here is a **form**, not JSON — `capture/routes.py` declares its
 * parameters with FastAPI's `Form(...)`, and the reason is the file upload on
 * `/api/capture/import`: a route that accepts `UploadFile` is multipart, so all
 * of them were written that way for consistency.
 *
 * The blank-means-leave-alone rule on `/api/capture/config` is why this drops
 * `undefined` rather than sending it as `"undefined"`: the operator changing the
 * pace on day four must not have to re-type a bot token to do it.
 */
function form(fields: Record<string, string | number | boolean | undefined | null>): FormData {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null) continue;
    fd.set(k, typeof v === 'boolean' ? (v ? '1' : '0') : String(v));
  }
  return fd;
}

export const getCaptureStatus = (signal?: AbortSignal) =>
  request<CaptureStatus>('/capture/status', {}, signal);

export const getCaptureTask = (signal?: AbortSignal) =>
  request<CaptureTask>('/capture/task', {}, signal);

export const getCaptureQueue = (
  args: { state?: string; limit?: number; offset?: number } = {},
  signal?: AbortSignal
) => request<CaptureQueueResponse>(`/capture/queue${qs(args)}`, {}, signal);

export const getCaptureActivity = (limit = 60, signal?: AbortSignal) =>
  request<{ ok: boolean; events: CaptureEvent[] }>(
    `/capture/activity${qs({ limit })}`,
    {},
    signal
  );

export const getCaptureFailures = (limit = 120, signal?: AbortSignal) =>
  request<{ ok: boolean; failures: CaptureFailure[] }>(
    `/capture/failures${qs({ limit })}`,
    {},
    signal
  );

export const getCaptureCollections = (signal?: AbortSignal) =>
  request<{ ok: boolean; collections: CaptureCollection[] }>(
    '/capture/collections',
    {},
    signal
  );

/** POST, not GET: it probes Telegram and the disk, so it is not idempotent. */
export const capturePreflight = () =>
  request<PreflightResponse>('/capture/preflight', { method: 'POST' });

/** Only the fields you pass are changed. Never send a token you did not type. */
export interface CaptureConfigFields {
  bot_token?: string;
  channel_id?: string | number;
  api_id?: string | number;
  api_hash?: string;
  cookies_text?: string;
  target_seconds?: number;
  quiet_hours?: boolean;
  breaks?: boolean;
  /** Comma-separated. An empty string is not "clear" — it is "leave alone". */
  skip_collections?: string;
  max_attempts?: number;
  gallery_dl?: boolean;
  /** `fast` or `safe`; anything else is ignored by the server as a typo. */
  speed?: string;
}

export const saveCaptureConfig = (fields: CaptureConfigFields) =>
  request<{ ok: boolean; settings: CaptureSettings }>('/capture/config', {
    method: 'POST',
    body: form(fields as Record<string, string | number | boolean | undefined>),
  });

export const startCapture = (seed_first = true) =>
  request<{ ok: boolean; state: string; message: string }>('/capture/start', {
    method: 'POST',
    body: form({ seed_first }),
  });

export const pauseCapture = () =>
  request<{ ok: boolean; state: string }>('/capture/pause', { method: 'POST' });

export const resumeCapture = () =>
  request<{ ok: boolean; state: string }>('/capture/resume', { method: 'POST' });

export const stopCapture = () =>
  request<{ ok: boolean; state: string }>('/capture/stop', { method: 'POST' });

// The three ways in. All three answer `{ok, started}` immediately and then run
// on the task thread — parsing a 20 MB export inline would park the event loop
// and freeze the status poll at the moment progress most needs to be visible.
export const importCaptureText = (text: string) =>
  request<{ ok: boolean; started: string }>('/capture/import', {
    method: 'POST',
    body: form({ text }),
  });

export const importCapturePath = (path: string) =>
  request<{ ok: boolean; started: string }>('/capture/import', {
    method: 'POST',
    body: form({ path }),
  });

export const importCaptureFile = (file: File) => {
  const fd = new FormData();
  fd.set('file', file);
  return request<{ ok: boolean; started: string }>('/capture/import', {
    method: 'POST',
    body: fd,
  });
};

/** Adopt everything the channel already holds, so nothing is captured twice. */
export const seedCaptureChannel = () =>
  request<{ ok: boolean; started: string }>('/capture/seed/channel', { method: 'POST' });

/** Forget the scan watermark: the next scan re-reads the channel from message 1. */
export const rescanCaptureChannel = () =>
  request<{ ok: boolean; message: string }>('/capture/seed/rescan', { method: 'POST' });

export const requeueCapture = (state = 'failed') =>
  request<{ ok: boolean; requeued: number }>('/capture/requeue', {
    method: 'POST',
    body: form({ state }),
  });

/** Push the ledger to the channel now, rather than waiting for the interval. */
export const snapshotCapture = () =>
  request<{ ok: boolean; message: string }>('/capture/snapshot', { method: 'POST' });

/**
 * Pull the pinned ledger out of the channel and put it in place.
 *
 * Destructive enough to belong on the Admin tab and not this one: the local
 * ledger is moved aside, not merged. The server refuses while a run is live.
 */
export const restoreCaptureLedger = () =>
  request<{ ok: boolean; counts: CaptureCounts; message: string }>('/capture/restore', {
    method: 'POST',
  });

/** A download, so it is an href rather than a fetch. */
export const captureExportUrl = () => `${API}/capture/export`;

export const getBackfillStatus = (signal?: AbortSignal) =>
  request<BackfillStatus>('/capture/backfill', {}, signal);

export const startBackfill = (limit = 0) =>
  request<{ ok: boolean; state: string; message: string }>('/capture/backfill/start', {
    method: 'POST',
    body: form({ limit }),
  });

export const stopBackfill = () =>
  request<{ ok: boolean; state: string }>('/capture/backfill/stop', { method: 'POST' });

// ── The native shell ──────────────────────────────────────────────────────
declare global {
  interface Window {
    pywebview?: {
      api: {
        pick_folder?: () => Promise<string>;
        open_home?: () => Promise<string>;
        open_path?: (p: string) => Promise<string>;
        open_url?: (u: string) => Promise<string>;
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

/**
 * Open a reel's permalink outside the app.
 *
 * Never an `<a href>`: inside the desktop window an external link navigates the
 * *application* to instagram.com and there is no back button. The bridge sends
 * it to the real browser instead, and refuses anything that is not an https
 * Instagram URL. Under `npm run dev` there is no bridge, so it falls back to
 * `window.open`, which in a tab is the correct behaviour anyway.
 */
export async function openUrl(url: string): Promise<void> {
  const bridge = window.pywebview?.api?.open_url;
  if (bridge) {
    try {
      await bridge(url);
    } catch {
      /* the bridge logs its own refusals */
    }
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}

export type { Facet };

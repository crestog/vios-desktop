/**
 * types.ts — the shapes the Python side actually returns.
 *
 * Written against the route bodies in `atlas/server.py` and
 * `server/desktop_routes.py`, not against what a REST API "should" look like.
 * Three conventions the backend follows everywhere, worth knowing before
 * reading any view:
 *
 *   - a list is `results`, never `items`, and carries `total` / `offset` /
 *     `limit` beside it;
 *   - a failure is `{ok: false, note: "..."}` with a 200 as often as not, so
 *     `ok` is checked rather than the HTTP status alone;
 *   - a number that was never measured is `null`, and `null` is not `0`.
 *     A video whose duration was never probed must not sort as the shortest.
 *
 * These are hand-written, and they stay hand-written. `npm run gen:api` does
 * regenerate `api/schema.d.ts` from the live OpenAPI document, but it cannot
 * check any of this: none of the 109 handlers annotate a `response_model`, so
 * every 200 in that document is `{}` and every response in the 5,515 lines it
 * emits is `unknown`. Nothing imports the file and it is gitignored. Annotating
 * the handlers would also fight the second convention above — a failure is
 * `{ok: false, note}` with a 200, which is not a second model.
 *
 * What *is* machine-checked is the half the document does describe. `npm run
 * audit:api` compares every URL `lib/api.ts` builds against every route the
 * running server mounts, and every query parameter against the ones it
 * declares. A renamed route is a 404 the build cannot see; a dropped filter is
 * worse, because `qs()` still sends it, FastAPI ignores what it did not declare,
 * and the answer is a 200 carrying every row. Response *shapes* are checked by
 * reading them.
 */

/** The seven evidence channels. Colour in this app means exactly one thing. */
export type ChannelKind =
  | 'speech'
  | 'ocr'
  | 'visual'
  | 'narrative'
  | 'style'
  | 'caption'
  | 'concept';

export const CHANNELS: ChannelKind[] = [
  'speech',
  'ocr',
  'visual',
  'narrative',
  'style',
  'caption',
  'concept',
];

/**
 * One passage of evidence about a span of a video — a row of `moments`.
 *
 * `t_start` / `t_end` are nullable because plenty of evidence is about the
 * whole reel rather than a moment in it (a caption, a style read). The
 * spectrum draws those as full-width rather than dropping them.
 */
export interface Moment {
  id: number;
  t_start: number | null;
  t_end: number | null;
  source: string;
  src_table?: string;
  text: string;
  score?: number;
  lex_rank?: number | null;
  dense_rank?: number | null;
}

/** A card. `/api/search` and `/api/library` both return rows of this shape. */
export interface VideoItem {
  video_key: string;
  title: string;
  caption?: string | null;
  creator?: string | null;
  category?: string | null;
  duration: number | null;
  width?: number | null;
  height?: number | null;
  likes?: number | null;
  created_at?: number | null;
  msg_id?: number | null;
  poster?: string | null;
  moment_count?: number | null;
  has_file?: boolean;
  sources?: Record<string, number> | string;

  // Search-only. `best` is the single strongest passage; `moments` are all of
  // this video's hits in time order, which is what the card's spectrum draws.
  rank?: number;
  score?: number;
  hit_count?: number;
  best?: Moment;
  moments?: Moment[];

  // Library-only: which half of the OR put this row in the results.
  matched?: 'meta' | 'inside' | 'both';
}

export interface Facet {
  value: string;
  count: number;
}

export interface SearchResponse {
  ok: boolean;
  query: string;
  results: VideoItem[];
  total: number;
  offset: number;
  limit: number;
  mode?: string;
  dense?: boolean;
  sort?: string;
  /** Before filters. `total` is after — "18 of 340, narrowed by your filters". */
  matched?: number;
  facets?: { creators: Facet[]; categories: Facet[] };
  filters?: Record<string, unknown>;
  candidates?: { lexical: number; dense: number; fused: number };
  took_ms?: number;
  note?: string;
}

export interface LibraryResponse {
  ok: boolean;
  results: VideoItem[];
  total: number;
  offset: number;
  limit: number;
  /** How many of these matched on *contents* rather than metadata. */
  inside?: number;
  note?: string;
}

export interface FacetsResponse {
  creators: Facet[];
  categories: Facet[];
  sources: Record<string, number>;
  totals: { videos: number; moments: number };
}

/** `/api/status` — what the archive is, right now. */
export interface ArchiveStatus {
  ok?: boolean;
  moments?: number;
  videos?: number;
  playable?: number;
  by_source?: Record<string, number>;
  dense_ready?: boolean;
  dense_count?: number;
  dense_model?: string | null;
  seconds?: number;
  creators?: number;
  [k: string]: unknown;
}

/**
 * `/api/status` — the composite envelope the server actually returns.
 *
 * Worth spelling out, because the archive counts live one level down: `search`
 * holds `videos` / `moments` / `playable`, and its siblings describe the
 * machinery around them. Reading `videos` off the top level yields `undefined`,
 * which `fmtCount` honestly prints as an em dash — so the bug looks like an
 * empty archive rather than like a wrong path, on a database that has rows.
 */
export interface StatusEnvelope {
  ok?: boolean;
  /** `idle | scanning | indexing | ready | error`, plus why. */
  boot?: {
    phase: string;
    detail: string;
    error: string;
    elapsed: number;
    [k: string]: unknown;
  };
  ingest?: Record<string, unknown>;
  index?: Record<string, unknown>;
  /** The archive counts every screen quotes. */
  search?: ArchiveStatus;
  graph?: GraphCounts;
  map?: Record<string, unknown>;
  bundles?: number;
  cache?: Record<string, unknown>;
  telegram?: { configured: boolean; missing: string[]; channel: number };
  [k: string]: unknown;
}

/** `/api/video/{key}` — every fact in the database about one reel. */
export interface VideoDetail {
  ok: boolean;
  video_key: string;
  meta: VideoItem & Record<string, unknown>;
  moments: Moment[];
  related: Array<{
    table: string;
    key: string;
    columns: string[];
    rows: Array<Record<string, unknown>>;
  }>;
  playback: {
    where: 'local' | 'cache' | 'remote' | string;
    size: number;
    via: string;
    msg_id?: number | null;
  };
}

/**
 * `/api/cell` — the provenance panel's payload.
 *
 * Note what this is and is not. It answers *what a value means and who else
 * says it* — role, declared type, whether search reads it and as what, the
 * row it points at, how many rows share it, which other tables carry it. It
 * does not carry a confidence float or a model name, because those are
 * columns in the row itself when the pass that wrote them recorded them, and
 * inventing them here would be the exact "number without provenance" this
 * product exists to avoid.
 */
export interface CellProvenance {
  ok: boolean;
  table: string;
  column: string;
  value: unknown;
  role: 'key' | 'start' | 'end' | 'content' | 'field';
  type: string;
  pk: number;
  indexed: boolean;
  /** Which observer this column's text comes from, when search reads it. */
  source: string | null;
  row: Record<string, unknown>;
  refers_to: { table: string; on: string; row: Record<string, unknown> } | null;
  same_value: number | null;
  elsewhere: Array<{ table: string; column: string; rows: number }>;
  video_key: string | null;
  video?: { video_key: string; title: string; duration: number | null };
  time_column: string | null;
  end_column: string | null;
  note?: string;
}

/** `/api/schema` */
export interface SchemaColumn {
  name: string;
  type: string;
  pk: number;
  role: 'key' | 'start' | 'end' | 'content' | 'field';
  source: string | null;
}

export interface SchemaTable {
  name: string;
  rows: number;
  key: string | null;
  start: string | null;
  end: string | null;
  /** Search reads this table's text. False for everything Atlas wrote itself. */
  indexed: boolean;
  /**
   * This table has a key and prose columns, so search would read it, and does
   * not — because Atlas wrote it. `moments` is the case worth labelling: it holds
   * every searchable passage in the archive and is not itself a source, so
   * `indexed: false` on it looks like a contradiction until you know that.
   * Absent on a table search skips for the ordinary reason of having no text.
   */
  own?: boolean;
  columns: SchemaColumn[];
}

export interface SchemaResponse {
  fingerprint?: string;
  tables: SchemaTable[];
}

/** `/api/table/{name}` — rows as arrays, so a wide table is not 400 keys deep. */
export interface TableResponse {
  ok: boolean;
  table: string;
  columns: string[];
  types: string[];
  rows: unknown[][];
  rowids: number[];
  total: number;
  offset: number;
  limit: number;
  note?: string;
}

/**
 * `/api/graph*`
 *
 * Four kinds and nothing else: `atlas/graph.py:rebuild()` writes `video`, `dim`,
 * `tag` and `hashtag`. `sub` means something different in each — the dimension
 * *table* for a dim, the *column* it was mined from for a tag — which is why
 * `lib/kinds.ts` and not a view decides how a node is labelled and coloured.
 *
 * `weight` on a node is its **summed degree**, not a confidence or a count of
 * anything a person would recognise: it is how much of the archive this node
 * connects. On an edge it is how many rows assert that link.
 */
export interface GraphNode {
  id: string;
  kind: string;
  label: string;
  sub?: string | null;
  weight: number;
  meta?: Record<string, unknown>;
}

export interface GraphEdge {
  src: string;
  dst: string;
  rel: string;
  weight: number;
  ref?: string | null;
}

/** `graph.counts()` — what the whole graph contains, not just what is drawn. */
export interface GraphCounts {
  nodes: number;
  edges: number;
  kinds: Record<string, number>;
  groups: Array<{ sub: string | null; kind: string; count: number }>;
}

/** `graph.status()` — the in-process build state, so a rebuild is visible. */
export interface GraphBuildStatus {
  phase: string;
  detail: string;
  nodes: number;
  edges: number;
  at: number;
}

export interface GraphResponse {
  ok: boolean;
  nodes: GraphNode[];
  edges: GraphEdge[];
  seeded_from?: string[];
  /** Only on `/api/graph` — the overview is the only caller that needs totals. */
  counts?: GraphCounts;
  status?: GraphBuildStatus;
  /** `/api/graph/expand` only: the node asked about, and what did not fit. */
  centre?: GraphNode;
  truncated?: number;
  total?: number;
  note?: string;
}

/** One table's worth of the literal rows behind a node or an edge. */
export interface RecordSet {
  table: string;
  rows: Array<Record<string, unknown>>;
}

/**
 * `/api/graph/node/{id}` — `graph.detail()`.
 *
 * `records` is the actual row, not a summary of it: for a dim node its own row,
 * for a tag the rows whose column contained the token. `videos` is what the
 * node reaches, heaviest edge first, already shaped like a card.
 */
export interface GraphNodeDetail {
  ok: boolean;
  node: GraphNode;
  records: RecordSet[];
  videos: VideoItem[];
  /** Set only when the node *is* a video. */
  video_key?: string;
  note?: string;
}

/**
 * `/api/graph/edge` — why two nodes are connected.
 *
 * `ref` is `table|column[|value]`, which is the stored reason the edge exists
 * and enough to rebuild the query that produced it. `records` is that query's
 * result, so a line in the graph leads to real rows rather than a tooltip
 * repeating what the line already showed.
 */
export interface GraphEdgeDetail {
  ok: boolean;
  src: GraphNode;
  dst: GraphNode;
  rel: string;
  weight: number;
  ref: string;
  records: RecordSet[];
  note?: string;
}

/**
 * `/api/graph/path` — the shortest chain between two nodes.
 *
 * `path` is ids in order; `nodes` is the same chain hydrated and re-sorted into
 * that order, because a path whose nodes are shuffled is not a path.
 */
export interface GraphPathResponse {
  ok: boolean;
  path?: string[];
  nodes?: GraphNode[];
  edges?: GraphEdge[];
  note?: string;
}

/**
 * `/api/roadmap` — `atlas/roadmap.py:plan()`.
 *
 * One passage to watch for a concept. `said` is the honest bit: `true` means
 * the transcript at this timecode literally contains the concept, `false` means
 * FTS found nothing and these are instead the strongest passages of the videos
 * that carry it — the right reels, not a pinpointed instant. A concept mined out
 * of a list column may never be spoken aloud anywhere, and implying a quote
 * that does not exist is exactly the failure this app is built against.
 *
 * `t_end` is never null here, unlike `Moment.t_end`: a point observation is
 * given `POINT_WIDTH_S` of width by the server so that it can be played.
 */
export interface RoadmapMoment {
  id: number;
  video_key: string;
  t_start: number;
  t_end: number;
  seconds: number;
  source: string;
  weight: number;
  text: string;
  said: boolean;
}

/**
 * Why one concept comes before another, in the numbers that decided it.
 *
 * `p_forward` is P(prerequisite | this), `p_back` is P(this | prerequisite),
 * and `strength` is `p_forward × (p_forward − p_back)`. The order is claimed
 * from the *gap* between the two, so both are shown rather than a single score:
 * "89% of reels about this also cover that, but only 31% the other way round"
 * is checkable, and a lone 0.27 is not.
 */
export interface RoadmapPrereq {
  id: string;
  label: string;
  shared: number;
  p_forward: number;
  p_back: number;
  strength: number;
}

export interface RoadmapEdge {
  src: string;
  dst: string;
  shared: number;
  p_forward: number;
  p_back: number;
  strength: number;
}

export interface RoadmapStep {
  id: string;
  label: string;
  kind: string;
  group?: string | null;
  /** 1 = needs nothing first. A step sits one past its deepest prerequisite. */
  level: number;
  videos: number;
  /** Sum of the goal search's scores; collapses to `videos` with no goal. */
  reach: number;
  share: number;
  keys: string[];
  prereq: RoadmapPrereq[];
  unlocks: Array<{ id: string; label: string }>;
  moments: RoadmapMoment[];
  seconds: number;
  said: boolean;
  /** `done` | `skip` | `''`. Laid over the cached plan on every request. */
  state: string;
  marked_at?: number;
}

/** A level of the DAG, with progress counted over the steps in it. */
export interface RoadmapStage {
  level: number;
  title: string;
  why: string;
  steps: string[];
  seconds: number;
  count: number;
  done: number;
  marked: number;
}

export interface RoadmapResponse {
  ok: boolean;
  goal: string;
  /** `archive` or `goal` — a goal matching under three videos falls back. */
  mode: string;
  scope_note: string;
  note: string;
  stages: RoadmapStage[];
  steps: RoadmapStep[];
  edges: RoadmapEdge[];
  videos: Record<string, VideoItem>;
  /** Steps whose every prerequisite is ticked off — what to watch next. */
  ready: string[];
  cached: boolean;
  stats: {
    concepts: number;
    stages: number;
    videos: number;
    minutes: number;
    moments: number;
    scope_videos: number;
    ordered?: number;
    done: number;
    skipped: number;
    marked: number;
    percent: number;
    remaining_minutes: number;
    ready: number;
  };
  built_ms: number;
}

/** `/api/roadmap/step/{id}` — the same concept with 24 passages instead of 5. */
export interface RoadmapStepDetail {
  ok: boolean;
  id: string;
  kind: string;
  label: string;
  group: string;
  /** The graph node's summed degree, not a confidence. */
  degree: number;
  state: string;
  marked_at: number;
  goal: string;
  mode: string;
  /** Across the archive, and within the goal's scope. Often different. */
  videos_total: number;
  videos_in_scope: number;
  keys: string[];
  moments: RoadmapMoment[];
  seconds: number;
  said: boolean;
  videos: Record<string, VideoItem>;
  note?: string;
}

export interface RoadmapGoal {
  label: string;
  kind: string;
  group: string;
  videos: number;
}

export interface RoadmapMarkResponse {
  ok: boolean;
  step_id?: string;
  state?: string;
  cleared?: number;
  counts: Record<string, number>;
  note?: string;
}

/** `/api/mirror/status` */
export interface MirrorStatus {
  running: boolean;
  paused: boolean;
  below_floor: boolean;
  total_videos: number;
  downloaded: number;
  derived: number;
  bytes_downloaded: number;
  active_downloads: Array<{
    key: string;
    msg_id?: number;
    got: number;
    total: number;
    percent: number;
    speed_kbps: number;
  }>;
  active_derives: Array<{ key: string; started_at: number }>;
  priority_queued: number;
  recent_errors?: Array<{ key: string; error: string; at: number }>;
  last_error?: string | null;
  disk: DiskUsage;
}

export interface DiskUsage {
  home: string;
  free_bytes: number;
  video_bytes: number;
  proxy_bytes: number;
  derived_bytes: number;
  model_bytes: number;
  db_bytes: number;
  free_floor_gb?: number;
  below_floor?: boolean;
}

/** `/api/desktop/host` — measured, never assumed. */
export interface HostFacts {
  ok: boolean;
  gpus: Array<{ index?: number; name: string; total_mb: number; free_mb: number }>;
  gpu_count: number;
  host: string;
  vram_total_mb: number;
  vram_free_mb: number;
  /** Free VRAM on the *smallest* card, minus headroom — what one model must fit. */
  usable_vram_mb: number;
  usable_vram_total_mb?: number;
  ram_total_mb: number;
  ram_available_mb: number;
  ram_known: boolean;
  usable_ram_mb?: number;
  disk_free_mb: number;
  cpus: number;
  // The architecture's own limits, from `resources.capabilities()`. These are
  // the real probe keys — an earlier `compute`/`flash_attn` pair never matched
  // what the server sends and always read undefined.
  compute_capability?: number;
  dtype?: string;
  bf16?: boolean;
  fp8?: boolean;
  flash_attention_2?: boolean;
  attention?: string;
  note?: string;
  [k: string]: unknown;
}

/** Watched local folders. */
export interface WatchedFolder {
  folder_id: number;
  path: string;
  added_at: number;
  last_scanned_at?: number | null;
  enabled: number;
}

export interface LocalVideo {
  file_hash: string;
  video_key: string;
  path: string;
  filename: string;
  size_bytes: number;
  mtime: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  status: 'active' | 'missing';
  first_indexed_at: number;
  last_seen_at: number;
}

/** `/api/engine/stats` */
/**
 * `/api/engine/stats` — the queue's counts, per state, plus what the worker is
 * doing right now.
 *
 * Seven states rather than the two the queue shipped with, because collapsing
 * them makes the strip unreadable as a report. A reel with no audio track and a
 * reel whose audio decoder crashed are not the same event: *4,800 done, 200
 * skipped* is a finished sweep, and *4,800 done, 200 failed* is an unsolved
 * problem.
 */
export interface EngineStats {
  pending: number;
  running: number;
  completed: number;
  /** Ran, correctly declined: no audio track, no shots yet, nothing to measure. */
  skipped: number;
  /** Asked to be retried later; `not_before` says when. Not an error. */
  deferred: number;
  failed: number;
  /** Cannot be hosted here at all — no GPU, no library, no runner. */
  unrunnable: number;
  current_job?: EngineJob | null;
  running_worker: boolean;
  paused: boolean;
  /** Component ids with an implementation on this machine. */
  runners?: number;
  /** Evidence rows imported but not yet in the search index. */
  index_pending?: number;
}

export interface EngineJob {
  job_id: number;
  video_key: string;
  component_id: string;
  state: string;
  attempts?: number;
  /**
   * Why the job is in the state it is, whatever that state is — the exception
   * for a failure, the explanation for a skip, the shortfall for a held pass.
   * Prefer this over {@link EngineJob.error}: four of the five terminal states
   * are not errors, and the server sends the same string under both names.
   */
  reason?: string;
  /** @deprecated The column's shipped name. `reason` is the same string. */
  error?: string | null;
  /** The pass's own findings — `{shots: 3, asl: 2.13, rhythm: 'metronomic'}`. */
  notes?: Record<string, unknown> | null;
  /** Evidence rows produced. 0 is a real answer: it ran and measured nothing. */
  rows?: number | null;
  /** Path of the shard holding those rows, under `paths.SHARD_DIR`. */
  shard?: string | null;
  /** A deferred job is not eligible until this epoch second. */
  not_before?: number | null;
  /** Live progress line while the pass is running; only on `current_job`. */
  detail?: string;
  created_at?: number;
  started_at?: number | null;
  finished_at?: number | null;
}

/**
 * `/api/engine/components` — the static pipeline catalogue, annotated with what
 * *this* machine can host.
 *
 * The queue tells you what is scheduled; this tells you what is possible, and
 * why. Two things read it. A job carries a bare `component_id` (`shots`,
 * `transcribe`) and this is where that resolves to a `title` and a `stage_name`.
 * And the `unrunnable` count in {@link EngineStats} gets its reason here: a pass
 * whose `vram_mb` exceeds the usable VRAM comes back `unrunnable: true` with the
 * shortfall in `reason`, rather than a bare number.
 *
 * `unrunnable` is the only field that moves — and only when free VRAM does — so
 * a view fetches this once per mount rather than polling it.
 */
export interface ComponentRow {
  id: string;
  title: string;
  /** Numeric stage index; `stage_name` is the label to show. */
  stage: number;
  stage_name: string;
  family: string;
  /** '' for structural passes that emit no claims — see {@link ChannelKind}. */
  channel: string;
  /** The HF repo or the library doing the work; '' for pure arithmetic. */
  model: string;
  device: 'cpu' | 'gpu' | string;
  /** 2 means it must shard across both cards — the reason a single GPU blocks it. */
  cards: number;
  vram_mb: number;
  disk_mb: number;
  /** Per video, on one T4, ~30 s reel. */
  seconds: number;
  tier: 'core' | 'deep' | 'optional' | string;
  default_on: boolean;
  summary: string;
  produces: string[];
  kinds: string[];
  /**
   * Whether an implementation of this pass exists on this machine at all.
   *
   * A different question from {@link ComponentRow.unrunnable}, and the two
   * together are what the row means: no runner is waiting on code, a runner plus
   * a reason is waiting on hardware or a library, and a runner with no reason
   * runs today.
   */
  runner?: boolean;
  unrunnable: boolean;
  /** Why it cannot run here, or null when it can. */
  reason: string | null;
}

export interface ComponentCatalogue {
  ok: boolean;
  /** False when the machine probe itself raised — runnability is then unknown. */
  measured: boolean;
  total: number;
  runnable: number;
  blocked: number;
  /** How many of `total` have an implementation on this machine. */
  runners?: number;
  defaults: number;
  components: ComponentRow[];
}

/** Sprite-sheet geometry, from `/api/derived/sprite/{key}/meta`. */
export interface SpriteMeta {
  key: string;
  sheet: string;
  cols: number;
  rows: number;
  count: number;
  tile_w: number;
  tile_h: number;
  interval: number;
  duration: number;
  cover_at: number;
  tiers: number[];
}

export interface KeyframeIndex {
  key: string;
  count: number;
  width: number;
  height: number;
  duration: number;
  scene_threshold: number;
  /** True when the extractor hit its per-video frame cap and stopped early. */
  capped: boolean;
  frames: Array<{ i: number; file: string; t: number | null }>;
}

export interface DerivedState {
  ok: boolean;
  key: string;
  have: { proxy: boolean; sprite: boolean; posters: boolean; keyframes: boolean };
  complete: boolean;
}

/* ═══════════════════════════════════════════════════════════════════════
   Capture — the acquisition side, from `capture/routes.py`.

   Two deviations from the conventions at the top of this file, both real and
   both worth knowing before writing against them:

     - a failure here is `{ok: false, error: "..."}`, not `note`. `capture/
       routes.py:_err` predates the atlas envelope and is used by all 24 routes;
     - `/api/capture/queue` returns `items`, not `results`. It is the one
       endpoint in the app that does, because it is a window into a ledger
       rather than a query result.

   The third thing: **no route here ever returns a credential.** `settings`
   carries `bot_token_set: true` and no token, by design — see the module
   docstring. Nothing in this file should tempt a view into asking for one.
   ═══════════════════════════════════════════════════════════════════════ */

/**
 * The six states a ledger row can be in — `capture/ledger.py:50-55`.
 *
 * `failed` and `unavailable` are the pair that must never be shown as one
 * thing: `failed` comes back on its own (there is a retry ladder and up to
 * three revivals behind it), `unavailable` means Instagram deleted the post and
 * no amount of waiting helps. A UI that lumps them together makes a healthy run
 * look broken.
 */
export type CaptureItemState =
  | 'queued'
  | 'fetching'
  | 'uploaded'
  | 'failed'
  | 'unavailable'
  | 'skipped';

export const CAPTURE_STATES: CaptureItemState[] = [
  'queued',
  'fetching',
  'uploaded',
  'failed',
  'unavailable',
  'skipped',
];

/**
 * `ledger.counts()` — one key per state *that exists*, plus two derived.
 *
 * A state with no rows is absent rather than zero, which is why every read of
 * this goes through `?? 0` at the call site. `remaining` is queued + failed +
 * fetching: what a run started now would still have to do.
 */
export interface CaptureCounts {
  total?: number;
  remaining?: number;
  [state: string]: number | undefined;
}

/** `pacing.py:describe()` — the rate limiter, showing its work. */
export interface CapturePacer {
  /** `fast` or `safe`. */
  profile: string;
  /** Seconds between requests it is aiming for. */
  target: number;
  floor: number;
  last_gap: number;
  /** Multiplier on `target`. Above 1 means Instagram pushed back. */
  backoff: number;
  /** Requests left before the next scheduled break. */
  until_break: number;
  quiet_hours: boolean;
  breaks: boolean;
  requests_per_minute: number;
}

/**
 * `creds.describe()` — presence and origin of each secret, never a value.
 *
 * `fields` is the whole point: one row per credential the app knows about, each
 * saying whether it is set and *where it came from*. "The token is missing" and
 * "the token came from the environment, not the file you just edited" are
 * different problems and a boolean cannot tell them apart.
 */
export interface StoredCredentials {
  fields?: Array<{
    name: string;
    label: string;
    description: string;
    aliases: string[];
    present: boolean;
    /** `env` | `kaggle` | `file` | `typed` | ''. */
    source: string;
  }>;
  /** All four Telegram secrets resolve to something. The rest are optional. */
  complete?: boolean;
  local_file?: string;
  local_file_present?: boolean;
  on_kaggle?: boolean;
  /**
   * Why the Kaggle secret store answered the way it did, and what to do about
   * it. Both are `''`/`[]` off Kaggle, which is every launch of this
   * application — they are declared because the panel renders them when
   * `on_kaggle` is true rather than guessing that it never is.
   */
  kaggle_reason?: string;
  kaggle_advice?: string[];
  kaggle_secrets_available?: boolean;
  [k: string]: unknown;
}

/** `engine.settings()` — presence and pace, never a secret. */
export interface CaptureSettings {
  base: string;
  ledger: string;
  bot_token_set: boolean;
  channel: number | string | null;
  api_credentials_set: boolean;
  /** Where each credential came from: env, stored file, or the form. */
  credential_sources: Record<string, string>;
  stored_credentials: StoredCredentials;
  cookies_set: boolean;
  speed: string;
  target_seconds: number;
  quiet_hours: boolean;
  breaks: boolean;
  skip_collections: string[];
  max_attempts: number;
  gallery_dl_fallback: boolean;
  snapshot_every: number;
}

/**
 * The item in flight. `{}` when nothing is.
 *
 * Every field is optional because the dict is built up as the item moves:
 * `key`/`url`/`phase`/`attempt`/`started` at the start, `bytes` once the fetch
 * lands, `sent`/`total` only while a multipart upload is running.
 */
export interface CaptureCurrent {
  key?: string;
  url?: string;
  /** `fetching` → `uploading` → `assets`. */
  phase?: string;
  attempt?: number;
  started?: number;
  bytes?: number;
  sent?: number;
  total?: number;
}

/**
 * The single background-task slot — a channel scan or an import.
 *
 * One at a time on purpose: both write the same ledger rows, and interleaving
 * them would make the result depend on timing. A second request gets a 409.
 */
export interface CaptureTask {
  kind: string;
  running: boolean;
  message: string;
  error: string;
  at: number;
}

/** `/api/capture/status` — `engine.status()` plus the task slot. */
/**
 * What the ledger knows about the channel's contents, from
 * `Ledger.seeded()`. The one fact that decides whether the queue can be
 * trusted.
 */
export interface CaptureSeed {
  seeded: boolean;
  /** `scanned` (read over MTProto) · `pasted` (a list of links, no message ids) · `''`. */
  how: string;
  /** When, as epoch seconds. 0 when never. */
  at: number;
  /** Highest channel message id the scan reached. */
  scanned_to: number;
  /** How many videos that scan found in the channel — a fact about the channel, not a row count. */
  in_channel: number;
}

export interface CaptureStatus {
  /** `idle` | `running` | `paused` | `stopping` | `error`. */
  state: string;
  message: string;
  error: string;
  counts: CaptureCounts;
  /**
   * Whether the ledger has ever been told what the channel already holds.
   * `counts` cannot answer this — an unseeded ledger and a finished one look
   * identical in it — and the engine refuses to capture while it is false,
   * because fetching against an empty ledger re-downloads and re-uploads
   * everything already in the channel.
   */
  seeded: CaptureSeed;
  session: {
    captured: number;
    failed: number;
    /** Seconds since this run started. */
    elapsed: number;
    started_at: number;
  };
  current: CaptureCurrent;
  /** Seconds left on a deliberate pause between requests. 0 when not waiting. */
  waiting_seconds: number;
  pacer: CapturePacer;
  eta_hours: number;
  /** Rows finished in the last hour, straight from the ledger. */
  per_hour: number;
  /** Consecutive rate-limit responses. Above zero means back off. */
  hostile_streak: number;
  settings: CaptureSettings;
  task: CaptureTask;
}

/** One ledger row, as the queue window selects it. */
export interface CaptureQueueRow {
  key: string;
  url: string;
  state: string;
  attempts: number | null;
  last_error: string | null;
  added_at: number | null;
  done_at: number | null;
  uploader: string | null;
  views: number | null;
  likes: number | null;
  file_size: number | null;
  duration: number | null;
  /** The Telegram message this landed in — the proof it is captured. */
  msg_id: number | null;
}

export interface CaptureQueueResponse {
  ok: boolean;
  /** `items`, not `results`. The one exception in the app. */
  items: CaptureQueueRow[];
  counts: CaptureCounts;
  offset: number;
  limit: number;
}

export interface CaptureCollection {
  name: string;
  /** Rows in this collection. */
  n: number;
  done: number;
}

export interface CaptureFailure {
  key: string;
  url: string;
  /** `failed` or `unavailable` — the distinction the whole list exists for. */
  state: string;
  attempts: number | null;
  last_error: string | null;
  last_try_at: number | null;
  next_try_at: number | null;
}

/** A row of the `event` table — the capture log. */
export interface CaptureEvent {
  id: number;
  at: number;
  kind: string;
  key: string | null;
  text: string | null;
}

export interface PreflightCheck {
  name: string;
  ok: boolean;
  detail: string;
}

/**
 * `/api/capture/preflight` — everything that could stop a week-long run.
 *
 * `ready` is the engine's verdict ("this would run"); `ok` is the envelope's
 * ("the request succeeded"). They are deliberately separate: a correctly
 * reported "you have no cookies" must not look like a server error.
 */
export interface PreflightResponse {
  ok: boolean;
  ready: boolean;
  checks: PreflightCheck[];
  /** Names of the failed checks that are *blocking*. Empty means go. */
  blocking: string[];
  counts: CaptureCounts;
  eta_hours: number;
  error?: string;
}

/**
 * `/api/capture/backfill` — the asset-set pass over videos captured before
 * clip sets existed. Separate worker, separate thread, its own state machine.
 */
export interface BackfillStatus {
  ok: boolean;
  state: string;
  message: string;
  error: string;
  started_at: number | null;
  done: number;
  failed: number;
  skipped: number;
  clips: number;
  uploads: number;
  /** How many rows this pass set out to do. */
  total: number;
  current: { key?: string; n?: number; of?: number; phase?: string };
  notes: string[];
  video_pause: number;
  autostart: { state: string; message: string; at: number; armed: boolean };
  /**
   * Archive-wide, so the card reads on its own. Carries `error` instead of the
   * counts when the ledger could not be opened — the worker's state is still
   * worth showing, and collapsing the two would blank the whole card.
   */
  counts: {
    videos?: number;
    with_assets?: number;
    without_assets?: number;
    clips?: number;
    error?: string;
  };
}

// ── Admin ─────────────────────────────────────────────────────────────────
// Three groups, matching `server/admin_routes.py`: the credential store, the
// wire contract with the other program, and restore. Nothing here re-states a
// shape another route already owns — the imported-sources list is
// `/api/bundles`, and there is no export type because there is no export route.

/**
 * What the credential form submits. Every field optional, and that is the
 * contract: an absent field means *leave that credential alone*, because the
 * form always renders empty on a machine that already has all six stored.
 * Sending `''` would be a different instruction, so the form must drop blanks
 * rather than pass them through.
 */
export interface CredentialFields {
  bot_token?: string;
  channel_id?: string;
  api_id?: string;
  api_hash?: string;
  hf_token?: string;
  ig_cookies?: string;
}

/**
 * `POST /api/admin/credentials`.
 *
 * `stored` and `changed` are different lists and the panel shows both: the
 * first is every field now in the file, the second only what this submission
 * touched. `exported` is the environment variables that were set in the same
 * call — the half that makes the value live in *this* process, which is why
 * saving does not need a restart.
 */
export interface CredentialSaveResult {
  ok: boolean;
  path: string;
  stored: string[];
  changed: string[];
  exported: string[];
  credentials: StoredCredentials;
}

/** `POST /api/admin/credentials/forget` — disk only; `effect` says so. */
export interface CredentialForgetResult {
  ok: boolean;
  removed: boolean;
  path: string;
  effect: string;
  credentials: StoredCredentials;
}

/**
 * `ahead` is the one that matters: the channel holds files written by a newer
 * processing plane than this build knows how to read. Everything else is
 * informational.
 */
export type WireVerdict = 'unknown' | 'empty' | 'ahead' | 'current' | 'behind';

/**
 * `GET /api/admin/wire` — can this build still read what the other program
 * writes?
 *
 * `schema.ours` is `sizing.SCHEMA_VERSION`, `highest_seen` is the largest
 * schema any file that actually imported carried, and `at_commit` is what
 * WIRE.md recorded when this tree was lifted. `wire_stale` is the quieter
 * failure of the three: the contract document no longer agreeing with the
 * constant in the tree means it was not updated with the code.
 */
export interface WireReport {
  ok: boolean;
  schema: {
    ours: number;
    highest_seen: number | null;
    at_commit: number | null;
    /** Imported files per schema number. `"unknown"` is a pre-header bundle. */
    by_schema: Record<string, number>;
  };
  verdict: WireVerdict;
  headline: string;
  wire_stale: boolean;
  /** Parsed out of WIRE.md, so `parsed: false` means it was reworded. */
  provenance: {
    upstream: string | null;
    commit: string | null;
    lifted_on: string | null;
    schema_at_commit: number | null;
    path: string;
    parsed: boolean;
    note?: string;
  };
  imported: {
    readable: boolean;
    bundles: number;
    shards: number;
    failed: number;
    bytes: number;
    newest_at: number | null;
  };
  note: string;
}

/**
 * One line of `plan.effects` — a store an apply either replaces or leaves
 * alone. `db_restore._effects` builds these before anything moves, and the
 * panel renders them verbatim: "restore the database" replaces two stores and
 * leaves three, and the three it leaves are why a restored session still has to
 * re-derive vectors before it behaves like the one that made the bundle.
 */
export interface RestoreEffect {
  target: string;
  /** `replaced` | `untouched`. */
  action: string;
  /** `yes` | `no` — a string, not a boolean, because the module writes one. */
  impact: string;
  detail: string;
}

/** What an inspect found: the bundle, against what is already here. */
export interface RestorePlan {
  seq: string | null;
  created_at: string | null;
  code_commit: string | null;
  schema: number | null;
  files: Array<{ name: string; parts: number; size: number }>;
  download_mb: number;
  bundle_counts: Record<string, number>;
  local_counts: Record<string, number>;
  /** Bundle posts minus local posts. Negative is the dangerous direction. */
  posts_delta: number | null;
  destructive: boolean;
  has_postgres: boolean;
  effects: RestoreEffect[];
  /** Present only after an apply: measured afterwards, not forecast. */
  outcome?: {
    loaded: string[];
    counts_after: Record<string, number>;
    matches_bundle: boolean | null;
    snapshot: string | null;
    next_steps: string[];
  };
}

/**
 * `GET /api/admin/restore`.
 *
 * `stalled_s` is the age of the last transferred byte, which is the number that
 * separates a slow download from a dead one. `missing` is recomputed per call,
 * so this stops saying "Telegram is not configured" one save later without a
 * restart. `scope` is the sentence the lifted module cannot say: the database
 * this replaces is not the database the reader reads.
 */
export interface RestoreStatus {
  ok: boolean;
  state: 'idle' | 'running' | 'ready' | 'done' | 'error';
  /** `inspect` | `apply` | `''`. */
  mode: string;
  stage: string;
  pct: number;
  detail: string;
  started_at: number | null;
  finished_at: number | null;
  plan: RestorePlan | null;
  error: string | null;
  log: string[];
  last_progress_at: number | null;
  stalled_s: number;
  /** Environment variable names, e.g. `VIOS_BOT_TOKEN`. */
  missing: string[];
  scope: string;
  target: string;
}

/** Both restore POSTs answer as soon as the thread is running. */
export interface RestoreStarted {
  ok: boolean;
  mode: string;
  seq?: string | null;
  scope: string;
}

/**
 * One row of `/api/bundles` — a bundle **or** a shard. They share the table and
 * are told apart by the `seq` prefix: `import_shard` writes `"shard:…"` into the
 * same eleven columns a manifest import uses. `status` is `ok` or `failed`, and
 * a failure is never settled — the next scan retries it, because the usual cause
 * is a torn download.
 */
export interface BundleRow {
  seq: string;
  manifest_id: number | null;
  schema: number | null;
  created_at: string | null;
  code_commit: string | null;
  parts: number | null;
  bytes: number | null;
  counts: Record<string, number>;
  imported_at: number | null;
  status: string;
  note: string | null;
}

/**
 * `/api/bundles` — note there is no `ok` key; this route predates that
 * convention and returns its two lists bare.
 */
export interface BundlesResponse {
  bundles: BundleRow[];
  /** Every column reflection put in the text index, and how it got there. */
  sources: Array<{
    table: string;
    text: string;
    /** The evidence kind, where a table labels its rows (`claim.channel`). */
    source: string | null;
    key: string;
    /** Name of the column carrying the moment's start, or null if untimed. */
    start: string | null;
    via: string | null;
  }>;
}

// ─────────────────────────────────────────────────────────────────────────────
// STUDIO
// ─────────────────────────────────────────────────────────────────────────────
// `studio.py`. Three read-only routes over the same four tables Search reads.
//
// The single most important thing about these types: **every number is
// nullable, and null does not mean zero.** `studio._stats` returns nulls for an
// empty sample and `_row_features` returns null for any rate whose denominator
// is missing, precisely so that a reel with no detected shots is absent from the
// cut-rate distribution rather than counted as having a cut rate of zero. A
// `?? 0` anywhere in the view would put that lie back.

/** `studio._stats` — the five-number summary plus dispersion. */
export interface Stats {
  /** How many reels (or shots, or slots) this summary is actually made of. */
  n: number;
  mean: number | null;
  median: number | null;
  p10: number | null;
  p90: number | null;
  min: number | null;
  max: number | null;
  /** Population standard deviation, not the sample estimate. */
  sd: number | null;
  /** σ/μ. Null when the mean is zero — σ/0 is not a large number. */
  cv: number | null;
}

/** One ranked term from the log-odds test. `z` is what the list is sorted by. */
export interface Phrase {
  term: string;
  z: number;
  log_odds: number;
  n_in: number;
  n_out: number;
  per_k_in: number;
  per_k_out: number;
}

export interface StudioScope {
  /** `archive` reads everything, `goal` ran a search, `filter` is creator/category. */
  mode: string;
  goal: string;
  creator: string;
  category: string;
  /** A sentence naming the scope, including any fallback that happened. */
  note: string;
  /** Total reels in the index, so a scope can be read as a fraction of it. */
  archive: number;
}

export interface Pacing {
  shots: number;
  cuts: number;
  /** Null when no shots were detected — not zero. */
  cuts_per_min: number | null;
  /** 1 − CV of shot length, clamped 0–1. A metronome scores 1. */
  regularity: number | null;
  shot_len: Stats;
  longest_hold: { t0: number; t1: number; len: number } | null;
  covered_s: number;
  /** Detected shot seconds over runtime. May exceed 1; deliberately unclamped. */
  coverage: number | null;
}

export interface ChannelPresence {
  source: string;
  moments: number;
  covered_s: number;
  /** Merged covered seconds over runtime. Exceeds 1 for whole-file channels. */
  share: number | null;
  first_at: number;
  last_at: number;
  words: number;
  chars: number;
  weight: number;
}

export interface StudioSection {
  n: number;
  bin0: number;
  bin1: number;
  t0: number;
  t1: number;
  len: number;
  share: number;
  /** Channel → mean occupancy 0–1 within this section. Zero entries dropped. */
  mix: Record<string, number>;
  /** e.g. `speech + caption-led`. A measurement, not a semantic name. */
  label: string;
  lead: string;
}

export interface StudioHook {
  window_s: number;
  moments: number;
  channels: string[];
  /** What is on screen at t=0 — stricter, and the question people mean. */
  first_frame_channels: string[];
  cuts: number;
  words: number;
  words_per_s: number | null;
  first_speech_at: number | null;
  silent_open: boolean;
  text: Array<{ source: string; t: number; text: string }>;
}

/**
 * `studio._video`. Deliberately *not* {@link VideoItem}: that one types
 * `sources` as `Record<string, number> | string` because search and library
 * each return it their own way, and `studio._video` splits it into a plain
 * `string[]`. Reusing VideoItem here would type this field as something it
 * never is.
 */
export interface StudioVideo {
  video_key: string;
  title: string;
  caption: string;
  creator: string;
  category: string;
  duration: number;
  width: number | null;
  height: number | null;
  fps: number;
  size_mb: number;
  likes: number | null;
  created_at: number | null;
  poster: string;
  moment_count: number;
  /** `{source: moments}` from `video_index.sources`, which stores JSON. */
  sources: Record<string, number>;
  has_speech: boolean;
  has_narrative: boolean;
  text_len: number;
}

export interface DeconstructResponse {
  ok: true;
  video: StudioVideo;
  duration: number;
  /** `index` | `evidence` | `unknown` — where the runtime came from. */
  duration_from: string;
  pacing: Pacing;
  channels: ChannelPresence[];
  timeline: {
    /** Column order for every row of `matrix`. */
    channels: string[];
    bins: number;
    bin_s: number;
    /** `[bin][channel]` occupancy 0–1. Empty when nothing is timed. */
    matrix: number[][];
  };
  sections: StudioSection[];
  hook: StudioHook;
  gaps: Array<{ t0: number; t1: number; len: number }>;
  claims: Array<{ kind: string; name: string; confidence: number; t0: number; t1: number }>;
  density: {
    words: number;
    words_per_s: number | null;
    ocr_chars_per_s: number | null;
    moments_per_min: number | null;
    channels_used: number;
  };
  moments: number;
  /** Plain sentences for every part of the answer that is thin, and why. */
  notes: string[];
}

/** Tertile cut points, or a stated reason there are none. */
export interface Band {
  ok: boolean;
  names: string[];
  edges: number[];
  why: string;
}

export interface Archetype {
  pace: string;
  talk: string;
  n: number;
  examples: Array<{
    video_key: string;
    title: string;
    cuts_per_min: number | null;
    speech_share: number | null;
  }>;
}

export interface ReelFeatures {
  video_key: string;
  title: string;
  creator: string;
  category: string;
  duration: number | null;
  shots: number;
  cuts_per_min: number | null;
  shot_len: number | null;
  regularity: number | null;
  moments: number;
  moments_per_min: number | null;
  words: number;
  words_per_s: number | null;
  speech_share: number | null;
  shares: Record<string, number | null>;
  channels: string[];
}

export interface PatternsResponse {
  ok: true;
  scope: StudioScope;
  reels: number;
  measures: {
    duration: Stats;
    cuts_per_min: Stats;
    shot_len: Stats;
    regularity: Stats;
    moments_per_min: Stats;
    words_per_s: Stats;
    speech_share: Stats;
  };
  channels: Array<{
    source: string;
    n: number;
    /** Share of reels carrying this channel at all. */
    rate: number | null;
    /** Distribution of runtime share among the reels that carry it. */
    share: Stats;
  }>;
  hook: {
    /** Denominator for every rate below. */
    reels: number;
    opens_with: Array<{ source: string; n: number; rate: number | null }>;
    leads_with: Array<{ source: string; n: number; rate: number | null }>;
    silent_open: { n: number; rate: number | null };
    words: Stats;
    cuts: Stats;
    first_speech_at: Stats;
    phrases: Phrase[];
    phrase_basis: { hook_terms: number; rest_terms: number };
  };
  bands: { pace: Band; talk: Band };
  archetypes: Archetype[];
  /** Capped at 60 — the per-reel table, not the whole scope. */
  reel_rows: ReelFeatures[];
  method: {
    phrases: string;
    bands: string;
    hook_window: number;
    compared: string;
  };
  notes: string[];
}

export interface Beat {
  name: string;
  /** Proportion of runtime this slot occupies, by convention. */
  p0: number;
  p1: number;
  /** The same slot in seconds, against the scope's median runtime. */
  t0: number;
  t1: number;
  len: number;
  lead: string | null;
  lead_rate: number | null;
  leads: Array<{ source: string; n: number; rate: number | null }>;
  /** How many reels voted on the lead — the denominator for `lead_rate`. */
  voters: number;
  cuts: Stats;
  words: Stats;
  phrases: Phrase[];
  examples: Array<{
    video_key: string;
    title: string;
    source: string;
    t: number;
    weight: number;
    text: string;
  }>;
}

export interface ScriptResponse {
  ok: true;
  scope: StudioScope;
  reels: number;
  /** Median runtime of the scope; every beat's seconds are a share of this. */
  target_s: number;
  duration: Stats;
  beats: Beat[];
  outline: {
    head: string;
    lines: Array<{ name: string; headline: string; points: string[] }>;
    /** The whole sheet as plain text, for copying. A rendering of `beats`. */
    text: string;
  };
  method: { slots: string; numbers: string; phrases: string; prose: string };
  notes: string[];
}



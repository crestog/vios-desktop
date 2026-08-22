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
 * These are hand-written for now. `npm run gen:api` regenerates
 * `api/schema.d.ts` from the live OpenAPI document, and the moment a view
 * disagrees with the server the build is where it should break.
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
  indexed: boolean;
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
export interface EngineStats {
  pending: number;
  running: number;
  completed: number;
  failed: number;
  unrunnable: number;
  current_job?: EngineJob | null;
  running_worker: boolean;
  paused: boolean;
}

export interface EngineJob {
  job_id: number;
  video_key: string;
  component_id: string;
  state: string;
  attempts?: number;
  error?: string | null;
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

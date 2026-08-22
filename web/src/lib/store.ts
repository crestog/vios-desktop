/**
 * lib/store.ts — the state that is *not* in the URL.
 *
 * The division is the whole design: anything a link should carry lives in the
 * address (see `lib/router.ts`), and what remains here is preference and live
 * telemetry — grid density, view mode, the drill-down panel, and the two
 * background workers' status. A query does not belong here; a column count
 * does not belong in a shared link.
 *
 * Hand-rolled rather than a state library, and this is the reason: the polling
 * status strip updates every three seconds, and with a context-based store
 * that re-renders every subscriber — including a five-thousand-card grid —
 * twenty times a minute. `useSyncExternalStore` with per-slice selectors means
 * the strip re-renders and the grid does not. That is a real frame-budget
 * decision, not a preference about dependencies.
 */

import { useCallback, useSyncExternalStore } from 'react';
import type { CellProvenance, DiskUsage, EngineStats, HostFacts, MirrorStatus } from '../types';
import { getEngineStats, getHost, getMirrorStatus, isAborted } from './api';

export type GridMode = 'contact' | 'grid' | 'list' | 'filmstrip';

export interface DrillTarget {
  table: string;
  column: string;
  rowid?: number;
  value?: string;
}

export interface AppState {
  /** 3–12 columns. Changes which poster *tier* is fetched, not just the CSS. */
  density: number;
  gridMode: GridMode;
  /** What the provenance panel is pointed at, or null when it is closed. */
  drill: DrillTarget | null;
  mirror: MirrorStatus | null;
  engine: EngineStats | null;
  host: HostFacts | null;
  disk: DiskUsage | null;
  /** Set when the local server stops answering, so the UI can say so once. */
  offline: string | null;
}

const PREFS_KEY = 'vios.prefs.v1';

function loadPrefs(): Partial<AppState> {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return {};
    const got = JSON.parse(raw) as Partial<AppState>;
    return {
      density: typeof got.density === 'number' ? clampDensity(got.density) : undefined,
      gridMode: got.gridMode,
    };
  } catch {
    return {};
  }
}

function clampDensity(n: number): number {
  return Math.min(12, Math.max(3, Math.round(n)));
}

const prefs = loadPrefs();

let state: AppState = {
  density: prefs.density ?? 5,
  gridMode: prefs.gridMode ?? 'grid',
  drill: null,
  mirror: null,
  engine: null,
  host: null,
  disk: null,
  offline: null,
};

const listeners = new Set<() => void>();

function set(patch: Partial<AppState>) {
  // Reference equality is what `useSyncExternalStore` compares, so a no-op
  // patch must not produce a new object — otherwise every poll tick re-renders
  // every subscriber even when nothing changed.
  let changed = false;
  for (const [k, v] of Object.entries(patch)) {
    if ((state as unknown as Record<string, unknown>)[k] !== v) {
      changed = true;
      break;
    }
  }
  if (!changed) return;
  state = { ...state, ...patch };
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export const store = {
  get: () => state,

  setDensity(n: number) {
    const density = clampDensity(n);
    set({ density });
    persist();
  },

  setGridMode(gridMode: GridMode) {
    set({ gridMode });
    persist();
  },

  openDrill(target: DrillTarget) {
    set({ drill: target });
  },

  closeDrill() {
    set({ drill: null });
  },
};

function persist() {
  try {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ density: state.density, gridMode: state.gridMode })
    );
  } catch {
    /* private mode, quota, whatever — a lost preference is not worth a dialog */
  }
}

/**
 * Subscribe to one slice.
 *
 * `selector` must return a stable reference for unchanged state — return a
 * field, not a fresh object. `useDensity()` re-renders on a density change and
 * ignores the mirror poll; `{density}` in a selector would re-render on both.
 */
export function useStore<T>(selector: (s: AppState) => T): T {
  const get = useCallback(() => selector(state), [selector]);
  return useSyncExternalStore(subscribe, get, get);
}

const pickDensity = (s: AppState) => s.density;
const pickGridMode = (s: AppState) => s.gridMode;
const pickDrill = (s: AppState) => s.drill;
const pickMirror = (s: AppState) => s.mirror;
const pickEngine = (s: AppState) => s.engine;
const pickHost = (s: AppState) => s.host;
const pickDisk = (s: AppState) => s.disk;
const pickOffline = (s: AppState) => s.offline;

export const useDensity = () => useStore(pickDensity);
export const useGridMode = () => useStore(pickGridMode);
export const useDrill = () => useStore(pickDrill);
export const useMirror = () => useStore(pickMirror);
export const useEngine = () => useStore(pickEngine);
export const useHost = () => useStore(pickHost);
export const useDisk = () => useStore(pickDisk);
export const useOffline = () => useStore(pickOffline);

// ── Telemetry polling ─────────────────────────────────────────────────────
// Two rates, because the two facts move at different speeds. Mirror progress
// and queue depth change second to second and drive the status strip. Hardware
// does not: the GPU's name never changes and its free VRAM only moves when a
// model loads, so probing it every three seconds would shell out to
// `nvidia-smi` twelve hundred times an hour to learn nothing.

const FAST_MS = 3000;
const SLOW_MS = 60_000;

let polling = false;

/**
 * One fast tick: mirror status + engine queue depth, the two facts that drive
 * the status strip. Hoisted out of `startPolling` so `refreshTelemetry()` can
 * fire the exact same read on demand — a control button (start/pause a worker)
 * calls it so its effect shows within one round-trip instead of on the next
 * three-second tick.
 *
 * Settled rather than all: the engine queue answering while the mirror is
 * mid-restart should still update the queue. One failing endpoint must not
 * blank the whole strip.
 */
async function pollFast(): Promise<void> {
  const [m, e] = await Promise.allSettled([getMirrorStatus(), getEngineStats()]);
  if (m.status === 'fulfilled') set({ mirror: m.value, disk: m.value.disk ?? state.disk });
  if (e.status === 'fulfilled') set({ engine: e.value });

  const down = [m, e].find(
    (r) => r.status === 'rejected' && !isAborted(r.reason) && r.reason?.status === 0
  );
  set({ offline: down ? 'the local server is not answering' : null });
}

export function startPolling(): void {
  if (polling) return;
  polling = true;

  const slow = async () => {
    try {
      set({ host: await getHost() });
    } catch {
      /* no GPU, no torch, no nvidia-smi — the strip says so from `gpus: []` */
    }
  };

  void pollFast();
  void slow();
  window.setInterval(() => void pollFast(), FAST_MS);
  window.setInterval(() => void slow(), SLOW_MS);
}

/** Re-read the workers now — after a start/pause/resume, so the button lands. */
export async function refreshTelemetry(): Promise<void> {
  await pollFast();
}

/** Re-read the machine now — after a model load, when free VRAM has moved. */
export async function refreshHost(): Promise<void> {
  try {
    set({ host: await getHost(true) });
  } catch {
    /* leave the last good reading up rather than blanking the panel */
  }
}

export type { CellProvenance };

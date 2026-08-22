/**
 * lib/router.ts — real URLs, because a moment has to be shareable.
 *
 * The plan is specific about this: the player is *a route, not a modal* —
 * `/watch/<key>?t=14.32&q=…` — "so a link shares the exact moment with markers
 * intact". That requirement rules out keeping the current view in a variable:
 * a modal has no address, cannot be pasted, and cannot be reached with Back.
 *
 * So the URL is the state, and this module is the only translator:
 *
 *     /                    → home
 *     /search?q=…&sort=…   → search, with its query in the address bar
 *     /library?q=…&mode=…  → library
 *     /watch/<key>?t=14.3  → player at a timestamp
 *     /graph?node=…        → graph, optionally focused
 *     /roadmap?goal=…
 *     /studio?key=…&scope=…
 *     /data?table=…
 *     /capture /engine /admin
 *
 * Deliberately hand-rolled rather than a router dependency. There are ten
 * routes and one dynamic segment; a library would add a build dependency and
 * a `<Provider>` to save about forty lines, and the parse below is the entire
 * cost. It uses the History API directly, so Back, Forward and a pasted link
 * all work by construction.
 */

export type ViewName =
  | 'home'
  | 'search'
  | 'library'
  | 'watch'
  | 'graph'
  | 'roadmap'
  | 'studio'
  | 'data'
  | 'capture'
  | 'engine'
  | 'admin';

/** The ten tabs, in nav order. `watch` is absent on purpose — it is a route. */
export const NAV_ORDER: ViewName[] = [
  'home',
  'search',
  'library',
  'graph',
  'roadmap',
  'studio',
  'data',
  'capture',
  'engine',
  'admin',
];

export interface Route {
  view: ViewName;
  /** Present only on `watch`. */
  key?: string;
  params: URLSearchParams;
}

/**
 * What every view takes, and all it takes.
 *
 * It lives here rather than in `App.tsx` so that a view importing its own props
 * type does not import the module that imports every view — a cycle that is
 * harmless at runtime with `import type` and confusing to read either way.
 */
export interface ViewProps {
  route: Route;
}

const VIEWS = new Set<string>([...NAV_ORDER, 'watch']);

/**
 * Where the app is mounted. FastAPI serves the built bundle from the root in
 * the desktop window and Vite serves it from the root in development, so this
 * is `''` today — but reading it from the document rather than assuming it
 * means a future mount under `/app/` needs no edit here.
 */
const BASE = '';

export function parse(loc: Location = window.location): Route {
  const path = loc.pathname.slice(BASE.length).replace(/^\/+|\/+$/g, '');
  const params = new URLSearchParams(loc.search);
  if (!path) return { view: 'home', params };

  const [head, ...rest] = path.split('/');

  if (head === 'watch') {
    // The key may itself be URI-encoded (a local key is `loc_<hex>`, a
    // Telegram key is digits, but a hand-edited URL can hold anything).
    const key = decodeURIComponent(rest.join('/') || '');
    return { view: 'watch', key, params };
  }

  if (VIEWS.has(head)) return { view: head as ViewName, params };

  // An unknown path is home rather than a 404 screen: there is no server-side
  // routing table to be out of sync with, so the only way to get here is a
  // typo or a stale bookmark, and neither deserves an error page.
  return { view: 'home', params };
}

export function href(view: ViewName, opts: { key?: string; params?: Record<string, unknown> } = {}): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(opts.params || {})) {
    if (v === undefined || v === null || v === '') continue;
    sp.set(k, String(v));
  }
  const q = sp.toString();
  const path =
    view === 'home'
      ? '/'
      : view === 'watch'
        ? `/watch/${encodeURIComponent(opts.key || '')}`
        : `/${view}`;
  return `${BASE}${path}${q ? `?${q}` : ''}`;
}

const listeners = new Set<(r: Route) => void>();

function announce() {
  const r = parse();
  listeners.forEach((fn) => fn(r));
}

export function onRouteChange(fn: (r: Route) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * Go somewhere. `replace` for state that changes as you type.
 *
 * The distinction matters more than it looks: pushing on every keystroke would
 * bury the previous *view* under thirty history entries, so Back would walk
 * back through "a", "ab", "abc" instead of returning to the library. Typing
 * replaces; clicking pushes.
 */
export function navigate(url: string, opts: { replace?: boolean } = {}): void {
  const current = `${window.location.pathname}${window.location.search}`;
  if (url === current) return;
  if (opts.replace) window.history.replaceState(null, '', url);
  else window.history.pushState(null, '', url);
  announce();
}

export function go(
  view: ViewName,
  opts: { key?: string; params?: Record<string, unknown>; replace?: boolean } = {}
): void {
  navigate(href(view, opts), { replace: opts.replace });
}

/** Open the player at a moment. The single most-linked action in the app. */
export function watch(key: string, t?: number, extra: Record<string, unknown> = {}): void {
  go('watch', {
    key,
    // Two decimals: enough to land on the right word in speech, short enough
    // that the URL stays readable when pasted into a note.
    params: { t: t !== undefined && t !== null ? Number(t).toFixed(2) : undefined, ...extra },
  });
}

export function start(): void {
  window.addEventListener('popstate', announce);
  // Intercept clicks on internal links so an `<a href>` behaves like a route
  // change rather than a full page load. Anchors are used rather than buttons
  // throughout so that middle-click and "copy link address" work — which is
  // half the point of having real URLs.
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
      return;
    }
    const anchor = (e.target as HTMLElement | null)?.closest?.('a');
    if (!anchor) return;
    const target = anchor.getAttribute('target');
    const raw = anchor.getAttribute('href');
    if (!raw || raw.startsWith('#') || (target && target !== '_self')) return;
    // External and protocol-relative links are left to the browser.
    if (/^[a-z]+:/i.test(raw) || raw.startsWith('//')) return;
    e.preventDefault();
    navigate(raw);
  });
}

/** Read one query param as a number, or undefined if absent/unparseable. */
export function num(params: URLSearchParams, name: string): number | undefined {
  const raw = params.get(name);
  if (raw === null || raw === '') return undefined;
  const n = Number(raw);
  return Number.isFinite(n) ? n : undefined;
}

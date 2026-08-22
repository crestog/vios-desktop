/**
 * components/Shell.tsx — the frame every view lives inside.
 *
 * One shell, one design language, one router — which is the structural fix for
 * "the tabs look like different products". In v1 there were six HTML documents
 * joined by four iframes and three hard page loads, so state could not be
 * shared and the chrome was drawn three times in three styles. Here the chrome
 * is drawn once and the view is a child.
 *
 * The tabs are `<a href>` elements, not buttons, and the nav list comes from
 * `NAV_ORDER` rather than a local array — so adding a view is one entry in the
 * router and cannot produce a tab that navigates nowhere, or a route with no
 * tab. `watch` is deliberately absent from that list: the player is a
 * destination you arrive at from a result, not a tab you visit empty.
 */

import { useEffect, useRef, useState } from 'react';
import {
  BarChart3,
  Boxes,
  Camera,
  Database,
  Home,
  Library,
  Map as MapIcon,
  Search,
  Settings,
  Share2,
  Wand2,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { Route, ViewName } from '../lib/router';
import { NAV_ORDER, go, href } from '../lib/router';
import { useDrill } from '../lib/store';
import DrillDown from './DrillDown';
import StatusBar from './StatusBar';

const ICON: Record<ViewName, LucideIcon> = {
  home: Home,
  search: Search,
  library: Library,
  graph: Share2,
  roadmap: MapIcon,
  studio: Wand2,
  data: Database,
  capture: Camera,
  engine: Boxes,
  admin: Settings,
  watch: BarChart3, // never rendered — `watch` is not in NAV_ORDER
};

const LABEL: Record<ViewName, string> = {
  home: 'Home',
  search: 'Search',
  library: 'Library',
  graph: 'Graph',
  roadmap: 'Roadmap',
  studio: 'Studio',
  data: 'Data',
  capture: 'Capture',
  engine: 'Engine',
  admin: 'Admin',
  watch: 'Player',
};

export default function Shell({ route, children }: { route: Route; children: React.ReactNode }) {
  const drill = useDrill();
  const box = useRef<HTMLInputElement | null>(null);
  const [ask, setAsk] = useState('');

  // Ctrl-K from anywhere. The one keyboard shortcut worth having in a
  // single-window app: every other action is one click from the nav.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        box.current?.focus();
        box.current?.select();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Keep the header box in step with the address bar, so arriving at
  // /search?q=hook from a link shows "hook" rather than an empty field.
  useEffect(() => {
    if (route.view === 'search') setAsk(route.params.get('q') || '');
  }, [route.view, route.params]);

  return (
    <div className="vios-shell">
      <header className="vios-header">
        <a className="brand" href={href('home')} title="VIOS — Video Intelligence OS">
          <span className="brand-dot" />
          <span className="brand-name">VIOS</span>
        </a>

        <nav className="vios-nav" aria-label="Main">
          {NAV_ORDER.map((v) => {
            const Icon = ICON[v];
            return (
              <a
                key={v}
                className={`nav-tab${route.view === v ? ' active' : ''}`}
                href={href(v)}
                aria-current={route.view === v ? 'page' : undefined}
                title={LABEL[v]}
              >
                <Icon size={14} />
                <span>{LABEL[v]}</span>
              </a>
            );
          })}
        </nav>

        <form
          className="header-ask"
          onSubmit={(e) => {
            e.preventDefault();
            const q = ask.trim();
            if (q) go('search', { params: { q } });
          }}
        >
          <Search size={13} />
          <input
            ref={box}
            className="header-ask-input"
            value={ask}
            onChange={(e) => setAsk(e.target.value)}
            placeholder="Ask the archive…"
            aria-label="Search the archive"
            spellCheck={false}
          />
          <kbd>Ctrl K</kbd>
        </form>
      </header>

      <main className="vios-content">{children}</main>

      <StatusBar />

      {drill && <DrillDown target={drill} />}
    </div>
  );
}

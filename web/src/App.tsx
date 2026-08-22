/**
 * App.tsx — the route switch, and nothing else.
 *
 * Eleven views, one `switch`. Two rules keep it that way:
 *
 *   - **Views are never imported by other views.** Every cross-view move is a
 *     route change, which is what makes every destination linkable and
 *     back-navigable. A view that imported another would create a second way to
 *     get somewhere that has no address.
 *
 *   - **The route is read once, here, and passed down.** A view reads its own
 *     params off the `Route` it is given rather than touching
 *     `window.location`, so a view is a pure function of (route, store) — which
 *     is the only reason eleven screens can share one shell without the state
 *     tangling.
 *
 * Deliberately not lazy-loaded. The whole bundle is a few hundred kilobytes off
 * local disk, so code-splitting would trade a guaranteed-fast first paint for
 * eleven chances at a loading spinner on a tab click.
 */

import { useEffect, useState } from 'react';
import Shell from './components/Shell';
import { onRouteChange, parse, type Route } from './lib/router';
import AdminView from './views/Admin';
import CaptureView from './views/Capture';
import DataView from './views/Data';
import EngineView from './views/Engine';
import GraphView from './views/Graph';
import HomeView from './views/Home';
import LibraryView from './views/Library';
import RoadmapView from './views/Roadmap';
import SearchView from './views/Search';
import StudioView from './views/Studio';
import WatchView from './views/Watch';

export default function App() {
  const [route, setRoute] = useState<Route>(() => parse());

  useEffect(() => onRouteChange(setRoute), []);

  // The window title follows the route, because this is a real window with a
  // taskbar entry — "VIOS" on every screen wastes the one label Windows shows.
  useEffect(() => {
    const name = route.view === 'home' ? '' : route.view;
    document.title = name ? `${name} · VIOS` : 'VIOS — Video Intelligence OS';
  }, [route.view]);

  return (
    <Shell route={route}>
      {route.view === 'home' && <HomeView route={route} />}
      {route.view === 'search' && <SearchView route={route} />}
      {route.view === 'library' && <LibraryView route={route} />}
      {route.view === 'watch' && <WatchView route={route} />}
      {route.view === 'graph' && <GraphView route={route} />}
      {route.view === 'roadmap' && <RoadmapView route={route} />}
      {route.view === 'studio' && <StudioView route={route} />}
      {route.view === 'data' && <DataView route={route} />}
      {route.view === 'capture' && <CaptureView route={route} />}
      {route.view === 'engine' && <EngineView route={route} />}
      {route.view === 'admin' && <AdminView route={route} />}
    </Shell>
  );
}

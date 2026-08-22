/**
 * main.tsx — the mount point, and the three things that must happen before it.
 *
 * Order matters here. `router.start()` installs the popstate and link handlers
 * *before* React renders, so the first paint already knows which view it is —
 * mounting home and then correcting to /watch would cost a wasted render and a
 * visible flash on a pasted link. `startPolling()` kicks the two telemetry
 * loops so the status strip has real numbers within the first tick rather than
 * dashes for three seconds.
 *
 * No `<StrictMode>`, and that is a considered choice rather than an omission:
 * its double-invoke of effects fires every `useFetch` twice in development,
 * which turns the network panel into a poor tool for the latency work this app
 * has budgets for. The unmount guards it exists to surface are already written
 * explicitly in `useFetch`.
 */

import { createRoot } from 'react-dom/client';
import App from './App';
import { start as startRouter } from './lib/router';
import { startPolling } from './lib/store';
import './styles/main.css';

startRouter();
startPolling();

const host = document.getElementById('root');
if (!host) throw new Error('index.html has no #root to mount into');
createRoot(host).render(<App />);

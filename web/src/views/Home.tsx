/**
 * views/Home.tsx — what the system knows, and one box to ask it.
 *
 * Every number on this screen is queried. That is not a small point: v1's home
 * page shipped with `4,812` and `68.4 GB` typed into the HTML, which looked
 * right on the machine they were typed on and lied everywhere else. If a count
 * is not available yet it shows `—` and the skeleton, never a plausible number.
 *
 * The backdrop is real posters from the archive at 6% opacity, drifting slowly.
 * It is decoration with a purpose — it is *your* archive, so the home screen of a
 * freshly restored install looks visibly different from a full one — and it is
 * built from posters the recent strip is fetching anyway, so it costs no extra
 * requests. `prefers-reduced-motion` stops the drift, and `aria-hidden` keeps it
 * out of the accessibility tree entirely.
 */

import { useEffect, useMemo, useState } from 'react';
import { ArrowRight, Cpu, HardDrive, Search as SearchIcon } from 'lucide-react';
import type { ViewProps } from '../lib/router';
import { go, href } from '../lib/router';
import { getFacets, getGraph, getLibrary, getStatus, posterUrl } from '../lib/api';
import { useEngine, useMirror } from '../lib/store';
import { useFetch } from '../lib/useFetch';
import { fmtBytes, fmtCompact, fmtCount, fmtDur, fmtPct, plural } from '../lib/format';
import { CHANNEL_MEANING, channelTally, channelVar, chipClass } from '../lib/channels';
import { nodeCss, nodeNote, nodeTypeLabel } from '../lib/kinds';
import Card from '../components/Card';

const RECENT = 18;

export default function HomeView(_props: ViewProps) {
  const [ask, setAsk] = useState('');

  const status = useFetch(getStatus, []);
  const recent = useFetch(
    (signal) => getLibrary({ sort: 'recent', limit: RECENT }, signal),
    []
  );
  const facets = useFetch(getFacets, []);
  const graph = useFetch((signal) => getGraph(28, signal), []);
  const mirror = useMirror();
  const engine = useEngine();

  // `/api/status` is an envelope: the counts are in `search`, and the sibling
  // blocks describe the machinery. Reading them off the top level is how this
  // screen once showed five em dashes over a database with rows in it.
  const s = status.data?.search;
  const items = recent.data?.results || [];
  const channels = useMemo(
    () => channelTally(s?.by_source || facets.data?.sources),
    [s, facets.data]
  );

  // Posters for the backdrop. Taken from the rows the recent strip already
  // fetched, so the wallpaper is free.
  const wall = useMemo(() => items.slice(0, 12).map((v) => posterUrl(v.video_key, 360)), [items]);

  // The concept cloud, sized by weight. Sorted by weight so the biggest ideas
  // are first rather than wherever the graph happened to return them.
  const concepts = useMemo(() => {
    const nodes = (graph.data?.nodes || []).filter((n) => n.kind !== 'video');
    const top = [...nodes].sort((a, b) => b.weight - a.weight).slice(0, 40);
    const max = top[0]?.weight || 1;
    return top.map((n) => ({ ...n, rel: Math.max(0.35, n.weight / max) }));
  }, [graph.data]);

  useEffect(() => {
    document.title = 'VIOS — Video Intelligence OS';
  }, []);

  const submit = () => {
    const q = ask.trim();
    if (q) go('search', { params: { q } });
  };

  // Hours are the right unit for an archive and the wrong one for a start: four
  // seconds of footage rounds to "0.0 h", which reads as an empty library. Under
  // an hour this says the clock time instead.
  const footage =
    !s?.seconds
      ? '—'
      : s.seconds >= 3600
        ? `${(s.seconds / 3600).toFixed(s.seconds < 36000 ? 1 : 0)} h`
        : fmtDur(s.seconds);

  return (
    <div className="view home">
      <div className="home-wall" aria-hidden="true">
        {wall.map((src, i) => (
          <img src={src} alt="" key={i} loading="lazy" decoding="async" draggable={false} />
        ))}
      </div>

      <div className="view-body home-body">
        <section className="hero">
          <h1 className="hero-h">
            Everything you saved, <em>searchable</em>.
          </h1>
          <p className="hero-p">
            Every spoken line, every word on screen, every caption, and what a model
            watching it said. Ask in your own words.
          </p>
          <div className="hero-ask">
            <SearchIcon size={16} className="hero-ask-icon" />
            <input
              className="hero-ask-input"
              value={ask}
              onChange={(e) => setAsk(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submit();
              }}
              placeholder="the reel where he explains the three-second hook…"
              spellCheck={false}
              autoFocus
              aria-label="Ask the archive"
            />
            <button className="btn hero-go" onClick={submit} disabled={!ask.trim()}>
              Search <ArrowRight size={13} />
            </button>
          </div>
          <div className="hero-eg">
            {['hook', 'lighting setup', 'why it went viral', 'call to action'].map((eg) => (
              <a key={eg} href={href('search', { params: { q: eg } })}>
                {eg}
              </a>
            ))}
          </div>
        </section>

        <section className="stat-row">
          <Stat
            label={s?.videos === 1 ? 'reel' : 'reels'}
            value={fmtCount(s?.videos)}
            loading={status.first && status.loading}
            to={href('library')}
          />
          <Stat
            label={s?.moments === 1 ? 'claim on record' : 'claims on record'}
            value={fmtCount(s?.moments)}
            loading={status.first && status.loading}
            note="one per passage of evidence — a transcript line, an OCR string, a description"
          />
          <Stat
            label="of footage"
            value={footage}
            loading={status.first && status.loading}
          />
          <Stat
            label={(s?.creators ?? facets.data?.creators?.length) === 1 ? 'creator' : 'creators'}
            value={fmtCount(s?.creators ?? facets.data?.creators?.length)}
            loading={status.first && status.loading}
          />
          <Stat
            label="playable here"
            // A share only when the numerator is a subset of the denominator.
            // `playable` counts video files on this disk, indexed or not, so a
            // half-rebuilt index can leave more files than reels — and "300%"
            // is a worse way to say that than the note below.
            value={
              s?.playable !== undefined && s?.videos && s.playable <= s.videos
                ? `${fmtCount(s.playable)} · ${fmtPct(s.playable, s.videos)}`
                : fmtCount(s?.playable)
            }
            loading={status.first && status.loading}
            note={
              s?.playable !== undefined && s?.videos && s.playable > s.videos
                ? `${fmtCount(s.playable)} video files are on this disk but only ` +
                  `${fmtCount(s.videos)} ${s.videos === 1 ? 'is' : 'are'} in the ` +
                  `index — the rest were imported before the last rebuild, or ` +
                  `their evidence never arrived`
                : 'the file is on this disk or reachable in the channel'
            }
          />
        </section>

        {channels.length > 0 && (
          <section>
            <div className="section-h">Where the evidence comes from</div>
            <div className="chan-bar">
              {channels.map(({ channel, count }) => {
                const total = channels.reduce((a, c) => a + c.count, 0) || 1;
                return (
                  <a
                    key={channel}
                    className="chan-seg"
                    style={{ flexGrow: count, background: channelVar(channel) }}
                    href={href('search', { params: { q: '*', source: channel } })}
                    title={`${channel}: ${plural(count, 'claim')} — ${CHANNEL_MEANING[channel]} (${fmtPct(
                      count,
                      total
                    )})`}
                  >
                    <span className="chan-label">{channel}</span>
                    <span className="chan-n">{fmtCompact(count)}</span>
                  </a>
                );
              })}
            </div>
            {s?.dense_ready !== undefined && (
              <div className="home-note">
                {s.dense_ready
                  ? `Semantic search is on — ${fmtCount(s.dense_count)} passages embedded${
                      s.dense_model ? ` with ${s.dense_model}` : ''
                    }.`
                  : 'Semantic search is off — searches are keyword-only until the embedding index is built.'}
              </div>
            )}
          </section>
        )}

        <section>
          <div className="section-h">
            Recently added
            <a className="section-link" href={href('library', { params: { sort: 'recent' } })}>
              all of it <ArrowRight size={11} />
            </a>
          </div>
          {recent.error ? (
            <div className="state-box err">
              <div className="head">Could not read the archive</div>
              <div>{recent.error}</div>
            </div>
          ) : recent.first && recent.loading ? (
            <div className="strip-lane">
              {Array.from({ length: 8 }, (_, i) => (
                <div className="skel" style={{ width: 132, aspectRatio: '9 / 16' }} key={i} />
              ))}
            </div>
          ) : items.length ? (
            <div className="strip-lane">
              {items.map((v) => (
                <div className="strip-cell" key={v.video_key} style={{ width: 132 }}>
                  <Card video={v} density={7} showText hoverClip />
                </div>
              ))}
            </div>
          ) : (
            <div className="state-box">
              <div className="head">Nothing here yet</div>
              <div>
                The archive is empty. <a href={href('admin')}>Restore the pinned bundle</a> from the
                channel, or <a href={href('capture')}>start capturing</a>.
              </div>
            </div>
          )}
        </section>

        {concepts.length > 0 && (
          <section>
            <div className="section-h">
              What it is about
              <a className="section-link" href={href('graph')}>
                open the graph <ArrowRight size={11} />
              </a>
            </div>
            <div className="cloud">
              {concepts.map((c) => (
                <a
                  key={c.id}
                  className="cloud-w"
                  href={href('graph', { params: { node: c.id } })}
                  style={{
                    fontSize: `${(11 + c.rel * 17).toFixed(1)}px`,
                    opacity: 0.5 + c.rel * 0.5,
                    color: nodeCss(c),
                  }}
                  title={`${nodeTypeLabel(c)} — ${nodeNote(c)} · connects ${plural(
                    c.weight,
                    'claim',
                    'claims',
                    fmtCompact
                  )}`}
                >
                  {c.label}
                </a>
              ))}
            </div>
          </section>
        )}

        <section>
          <div className="section-h">Right now on this machine</div>
          <div className="tiles">
            <a className="tile" href={href('engine')}>
              <div className="tile-h">
                <Cpu size={13} /> Engine
              </div>
              {engine ? (
                <>
                  <div className="tile-big">
                    {fmtCount(engine.pending)} <span className="dim">waiting</span>
                  </div>
                  <div className="tile-sub">
                    {engine.running_worker ? 'worker running' : 'worker idle'}
                    {engine.paused ? ' · paused' : ''} · {fmtCount(engine.completed)} done ·{' '}
                    {fmtCount(engine.failed)} failed
                  </div>
                  {engine.current_job && (
                    <div className="tile-sub">
                      on <code>{engine.current_job.component_id}</code>
                    </div>
                  )}
                </>
              ) : (
                <div className="tile-sub">no queue reading yet</div>
              )}
            </a>

            <a className="tile" href={href('admin')}>
              <div className="tile-h">
                <HardDrive size={13} /> Mirror
              </div>
              {mirror ? (
                <>
                  <div className="tile-big">
                    {fmtPct(mirror.downloaded, mirror.total_videos || 1)}{' '}
                    <span className="dim">on disk</span>
                  </div>
                  <div className="tile-sub">
                    {fmtCount(mirror.downloaded)} of {fmtCount(mirror.total_videos)} ·{' '}
                    {fmtCount(mirror.derived)} derived · {fmtBytes(mirror.bytes_downloaded)} pulled
                  </div>
                  <div className="tile-sub">
                    {mirror.running
                      ? mirror.paused
                        ? 'paused'
                        : `${mirror.active_downloads.length} downloading`
                      : 'not running'}
                    {mirror.below_floor ? ' · disk floor reached' : ''} ·{' '}
                    {fmtBytes(mirror.disk?.free_bytes)} free
                  </div>
                </>
              ) : (
                <div className="tile-sub">no mirror reading yet</div>
              )}
            </a>

            <a className="tile" href={href('data')}>
              <div className="tile-h">Raw database</div>
              <div className="tile-big">
                {fmtCount(s?.moments)} <span className="dim">rows of evidence</span>
              </div>
              <div className="tile-sub">
                Every cell is a link to who said it, when, and the SQL that proves it.
              </div>
            </a>
          </div>
        </section>

        {facets.data?.creators?.length ? (
          <section>
            <div className="section-h">Who you watch</div>
            <div className="facet-chips">
              {facets.data.creators.slice(0, 24).map((c) => (
                <a
                  key={c.value}
                  className={chipClass('meta')}
                  href={href('library', { params: { creator: c.value } })}
                >
                  {c.value || '(unattributed)'} <span className="facet-v-n">{fmtCount(c.count)}</span>
                </a>
              ))}
            </div>
          </section>
        ) : null}

        <div className="home-foot">
          <span>
            {s?.videos ? `${plural(s.videos, 'reel')} · ` : ''}
            {s?.seconds ? `${fmtDur(s.seconds)} of footage · ` : ''}
            all of it on this machine
          </span>
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  note,
  loading,
  to,
}: {
  label: string;
  value: string;
  note?: string;
  loading?: boolean;
  to?: string;
}) {
  const body = (
    <>
      <div className="stat-v">{loading ? <span className="skel stat-skel" /> : value}</div>
      <div className="stat-l">{label}</div>
    </>
  );
  return to ? (
    <a className="stat" href={to} title={note}>
      {body}
    </a>
  ) : (
    <div className="stat" title={note}>
      {body}
    </div>
  );
}

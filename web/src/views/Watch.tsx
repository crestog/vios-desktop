/**
 * views/Watch.tsx — the player, which is a route and not a modal.
 *
 * `/watch/<key>?t=14.32&q=…` — so a moment can be pasted into a note, opened in
 * a second window with a middle click, and reached with Back. A modal has no
 * address, which is why v1 could show you a moment and never let you send it to
 * anyone.
 *
 * **This screen is the fix for the v17 complaint** — *"the place where frames
 * were shown was getting so small that anything was not visible."* The cause was
 * three columns competing for width: video, frames, evidence. Here the 9:16 video
 * is the anchor and gets the height; the frame track is a **full-width
 * horizontal scroller beneath it** rather than a narrow column beside it; and
 * the evidence list is a **collapsible right rail** that can be closed
 * completely. Nothing has to shrink for something else to be legible.
 *
 * Four mechanisms, each with a latency budget behind it:
 *
 *   - **First frame in under 150 ms** comes from the mirror's `-movflags
 *     +faststart` proxy: the moov atom is at the front, so the browser can start
 *     decoding without reading the whole file. Nothing here can achieve that if
 *     the derive has not run — hence the honest banner when it has not.
 *
 *   - **Seek in under 100 ms** comes from the proxy's ~1 s GOP. Also not this
 *     file's doing; this file only avoids getting in the way, by setting
 *     `currentTime` directly instead of reloading the element.
 *
 *   - **Scrub preview in under 16 ms, with zero requests**, is this file's job:
 *     one sprite-sheet JPEG is fetched once, and moving the pointer changes a
 *     `background-position`. No decode, no network, no thumbnail endpoint.
 *
 *   - **The playhead updates on a rAF loop throttled to 20 Hz.** `timeupdate`
 *     fires about four times a second, which makes a marker track visibly
 *     stutter; 60 Hz would re-render the frame strip sixty times a second for
 *     two pixels of movement. 50 ms is the point where it reads as continuous
 *     and costs nothing.
 *
 * One deliberate non-behaviour: **seeking does not rewrite the URL.** `?t=` is
 * where you came in, and rewriting it on every scrub would either bury the
 * previous view under history entries or silently change the link you are about
 * to copy. "Copy link at 14.32" reads the live time instead.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDownToLine,
  ChevronLeft,
  Copy,
  Link2,
  PanelRightClose,
  PanelRightOpen,
  Pause,
  Play,
  Volume2,
  VolumeX,
} from 'lucide-react';
import type { Moment, SpriteMeta } from '../types';
import type { ViewProps } from '../lib/router';
import { href, num } from '../lib/router';
import {
  getDerivedState,
  getKeyframes,
  getSpriteMeta,
  getVideo,
  playUrl,
  posterUrl,
  prioritizeMirror,
  spriteUrl,
} from '../lib/api';
import { useFetch } from '../lib/useFetch';
import { fmtBytes, fmtCompact, fmtT, clip, plural } from '../lib/format';
import { ALL_CHANNELS, channelOf, chipClass, type ChannelName } from '../lib/channels';
import { store } from '../lib/store';
import Spectrum from '../components/Spectrum';
import Mark from '../components/Mark';
import FrameTrack from '../components/FrameTrack';

/** 50 ms → 20 Hz. See the header note on why not 4 Hz and not 60. */
const HEAD_MS = 50;

/**
 * The scrub preview's display width, matching `FRAME_W` in FrameTrack so the
 * popup and the strip below it show the same size picture, and matching
 * `.scrub-tile-none`'s width in main.css so the popup is the same size whether
 * or not the sheet exists. `POP_PAD` is `.scrub-pop`'s 4 px padding plus its 1 px
 * border on both sides — needed to keep the popup inside the track, and the one
 * number here that a stylesheet edit can silently invalidate.
 */
const POP_W = 108;
const POP_PAD = 10;

export default function WatchView({ route }: ViewProps) {
  const key = route.key || '';
  const q = route.params.get('q') || '';
  const entryT = num(route.params, 't');

  const vid = useRef<HTMLVideoElement | null>(null);
  const [at, setAt] = useState(entryT ?? 0);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [drawer, setDrawer] = useState(true);
  const [only, setOnly] = useState<ChannelName | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  /**
   * The length of the file actually being decoded, once the decoder reports it.
   *
   * This outranks `videos.duration` deliberately. Everything on this screen is a
   * projection onto one axis — where each band sits in the spectrum, where the
   * playhead is, what second a scrub x maps to — and the metadata row is a
   * number *copied* from whatever wrote it, while this one is *measured* from
   * the bytes on screen. When they disagree every band is misplaced by the
   * ratio: the archive's `testkey` says 4.0 and its proxy decodes to 6.41, which
   * draws a claim ending at 4.0 s hard against the right edge of a bar that runs
   * to 6.4 — 62% of the way along. Deriving the timeline from the thing being
   * played is the only version that cannot drift.
   */
  const [fileDur, setFileDur] = useState<number | null>(null);
  const seeded = useRef(false);

  const detail = useFetch((signal) => getVideo(key, true, signal), [key]);
  const sprite = useFetch((signal) => getSpriteMeta(key, signal), [key]);
  const keys = useFetch((signal) => getKeyframes(key, signal), [key]);
  const derived = useFetch((signal) => getDerivedState(key, signal), [key]);

  const meta = detail.data?.meta;
  const moments = detail.data?.moments || [];
  const duration = fileDur ?? meta?.duration ?? sprite.data?.duration ?? null;
  const where = detail.data?.playback?.where;

  /** Whatever the element currently knows, if it is a usable number. */
  const readDur = useCallback(() => {
    const d = vid.current?.duration;
    if (typeof d === 'number' && Number.isFinite(d) && d > 0) setFileDur(d);
  }, []);

  // A fresh key is a different reel: the entry timestamp has to be applied
  // again, and the old one must not leak into it.
  useEffect(() => {
    seeded.current = false;
    setAt(entryT ?? 0);
    setSaid(null);
    setFileDur(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const seek = useCallback((t: number) => {
    const el = vid.current;
    if (!el) return;
    // Assigning currentTime on a short-GOP faststart proxy lands in well under
    // 100 ms. Reloading the element with a #t= fragment would not.
    el.currentTime = Math.max(0, t);
    setAt(Math.max(0, t));
  }, []);

  // The playhead loop. Runs only while playing, so a paused player costs nothing.
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let last = 0;
    const tick = (now: number) => {
      raf = requestAnimationFrame(tick);
      if (now - last < HEAD_MS) return;
      last = now;
      const el = vid.current;
      if (el) setAt(el.currentTime);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  // Keyboard, on the window rather than the element: the video only has focus
  // if you clicked it, and space-to-play has to work after clicking a passage.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = vid.current;
      if (!el) return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      const step = e.shiftKey ? 1 : 5;
      switch (e.key) {
        case ' ':
        case 'k':
          e.preventDefault();
          if (el.paused) void el.play();
          else el.pause();
          break;
        case 'ArrowLeft':
        case 'j':
          e.preventDefault();
          seek(el.currentTime - step);
          break;
        case 'ArrowRight':
        case 'l':
          e.preventDefault();
          seek(el.currentTime + step);
          break;
        case ',':
          e.preventDefault();
          seek(el.currentTime - 1 / 30);
          break;
        case '.':
          e.preventDefault();
          seek(el.currentTime + 1 / 30);
          break;
        case 'm':
          setMuted((v) => !v);
          break;
        case 'Home':
          seek(0);
          break;
        case 'Escape':
          setDrawer(false);
          break;
        default:
          break;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [seek]);

  const shown = useMemo(
    () => (only ? moments.filter((m) => channelOf(m.source || m.src_table) === only) : moments),
    [moments, only]
  );

  const tally = useMemo(() => {
    const counts = new Map<ChannelName, number>();
    for (const m of moments) {
      const c = channelOf(m.source || m.src_table);
      counts.set(c, (counts.get(c) || 0) + 1);
    }
    return ALL_CHANNELS.filter((c) => counts.has(c)).map((c) => ({
      channel: c,
      count: counts.get(c) || 0,
    }));
  }, [moments]);

  if (!key) {
    return (
      <div className="state-box">
        <div className="head">No reel in the address</div>
        <div>
          A player URL looks like <code>/watch/&lt;key&gt;?t=14.32</code>.{' '}
          <a href={href('library')}>Open the library</a> to pick one.
        </div>
      </div>
    );
  }

  return (
    <div className="view watch">
      <div className="view-bar">
        <button className="btn-ghost" onClick={() => window.history.back()} title="back">
          <ChevronLeft size={13} />
        </button>
        <span className="view-title watch-title">{meta?.title || key}</span>
        {meta?.creator && (
          <a className="watch-creator" href={href('library', { params: { creator: meta.creator } })}>
            {meta.creator}
          </a>
        )}
        <span className="spacer" />
        {where && (
          <span
            className={`sb-seg ${where === 'remote' ? 'sb-warn' : 'sb-ok'}`}
            title={
              where === 'remote'
                ? 'not on this disk yet — it streams from the channel while it downloads'
                : 'on this disk'
            }
          >
            {where}
            {detail.data?.playback?.size ? ` · ${fmtBytes(detail.data.playback.size)}` : ''}
          </span>
        )}
        <button
          className="btn-ghost"
          onClick={() => {
            const url = `${window.location.origin}${href('watch', {
              key,
              params: { t: at.toFixed(2), q },
            })}`;
            void navigator.clipboard?.writeText(url);
            setSaid(`link copied at ${fmtT(at)}`);
          }}
          title="copy a link that opens at this exact moment"
        >
          <Link2 size={12} /> {fmtT(at)}
        </button>
        <button
          className="btn-ghost"
          onClick={() => setDrawer((v) => !v)}
          title={drawer ? 'hide the evidence' : 'show the evidence'}
        >
          {drawer ? <PanelRightClose size={13} /> : <PanelRightOpen size={13} />}
        </button>
      </div>

      {derived.data && !derived.data.complete && (
        <div className="watch-warn">
          This reel has not been fully derived yet
          {derived.data.have && (
            <>
              {' '}
              (
              {(['proxy', 'sprite', 'posters', 'keyframes'] as const)
                .filter((k) => !derived.data!.have[k])
                .join(', ')}{' '}
              missing)
            </>
          )}
          . Playback and scrubbing will be slower until the mirror finishes it —
          <a href={href('engine')}> see the queue</a>.
        </div>
      )}

      <div className={`watch-body${drawer ? ' with-drawer' : ''}`}>
        <div className="watch-main">
          <div className="watch-stage">
            <video
              ref={vid}
              className="watch-video"
              src={playUrl(key)}
              poster={posterUrl(key, 720)}
              controls={false}
              muted={muted}
              playsInline
              preload="metadata"
              onLoadedMetadata={() => {
                readDur();
                if (seeded.current) return;
                seeded.current = true;
                if (entryT !== undefined && entryT > 0) seek(entryT);
                void vid.current?.play().catch(() => {
                  /* autoplay refused with sound — the play button still works */
                });
              }}
              /* Fires separately from `loadedmetadata` when a container reports its
                 length late, and it is the event that turns an initial `Infinity`
                 into a real number on a progressively-served file. */
              onDurationChange={readDur}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onTimeUpdate={() => {
                // Belt and braces: the rAF loop is off while paused, and a
                // programmatic seek while paused still has to move the marker.
                if (!playing && vid.current) setAt(vid.current.currentTime);
              }}
              onClick={() => {
                const el = vid.current;
                if (!el) return;
                if (el.paused) void el.play();
                else el.pause();
              }}
            />
          </div>

          <div className="watch-under">
            <div className="watch-controls">
              <button
                className="btn-icon"
                onClick={() => {
                  const el = vid.current;
                  if (!el) return;
                  if (el.paused) void el.play();
                  else el.pause();
                }}
                title={playing ? 'pause (space)' : 'play (space)'}
              >
                {playing ? <Pause size={14} /> : <Play size={14} />}
              </button>
              <button
                className="btn-icon"
                onClick={() => setMuted((v) => !v)}
                title={muted ? 'unmute (m)' : 'mute (m)'}
              >
                {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
              </button>
              {/* `fmtT` on both halves, not `fmtT` / `fmtDur`. They disagree about
                  what a short reel is: `fmtT(6.4)` is "6.4s" and `fmtDur(6.4)` is
                  "0:06", so the pair rendered as "1.0s / 0:06" — two scales on one
                  axis, and the reader has to convert one of them to know how far
                  through they are. `fmtT` already switches to m:ss above a minute,
                  so it covers both a six-second reel and a four-minute one. */}
              <span className="watch-time">
                {fmtT(at)} <span className="dim">/ {duration === null ? '—' : fmtT(duration)}</span>
              </span>
              <span className="spacer" />
              {said && <span className="watch-said">{said}</span>}
              {where === 'remote' && (
                <button
                  className="btn"
                  onClick={async () => {
                    try {
                      const r = await prioritizeMirror(key);
                      setSaid(r.note || r.state);
                    } catch (e) {
                      setSaid(String((e as Error).message || e));
                    }
                  }}
                >
                  <ArrowDownToLine size={12} /> Download now
                </button>
              )}
            </div>

            {/*
              The marker track. Same component as the 4 px band under a card —
              built once, used in both places, as the design requires. At full
              width it is also the scrub bar, and the sprite sheet is what makes
              hovering it instant.
            */}
            <ScrubTrack
              moments={shown}
              duration={duration}
              at={at}
              onSeek={seek}
              spriteKey={key}
              sprite={sprite.data}
            />
          </div>

          <FrameTrack
            videoKey={key}
            frames={keys.data?.frames}
            capped={keys.data?.capped}
            error={keys.error}
            loading={keys.first && keys.loading}
            at={at}
            duration={duration}
            onSeek={seek}
          />
        </div>

        {drawer && (
          <aside className="watch-drawer" aria-label="Evidence">
            <div className="rail-head">
              <span className="view-title">Evidence</span>
              <span className="spacer" />
              <span className="view-sub">{plural(moments.length, 'moment', 'moments', fmtCompact)}</span>
            </div>

            {tally.length > 0 && (
              <div className="watch-filters">
                <button
                  className={`facet-v${only === null ? ' on' : ''}`}
                  onClick={() => setOnly(null)}
                >
                  all
                </button>
                {tally.map(({ channel, count }) => (
                  <button
                    key={channel}
                    className={`${chipClass(channel)}${only === channel ? ' on' : ''}`}
                    onClick={() => setOnly(only === channel ? null : channel)}
                    title={`${plural(count, 'claim')} from ${channel}`}
                  >
                    {channel} <span className="facet-v-n">{count}</span>
                  </button>
                ))}
              </div>
            )}

            {detail.error ? (
              <div className="state-box err">
                <div className="head">Could not read this reel's evidence</div>
                <div>{detail.error}</div>
              </div>
            ) : detail.first && detail.loading ? (
              <div className="rail-body">
                {Array.from({ length: 8 }, (_, i) => (
                  <div className="skel" style={{ height: 34, marginBottom: 6 }} key={i} />
                ))}
              </div>
            ) : (
              <div className="rail-body">
                <PassageList moments={shown} q={q} at={at} onSeek={seek} />

                {meta?.caption && (
                  <>
                    <div className="section-h">The creator's caption</div>
                    <p className="rail-caption">
                      <Mark text={String(meta.caption)} q={q} />
                    </p>
                  </>
                )}

                {(detail.data?.related || []).map((rel) => (
                  <section key={rel.table}>
                    <div className="section-h">
                      {rel.table}
                      <a
                        className="section-link"
                        href={href('data', { params: { table: rel.table, q: key } })}
                      >
                        open the table
                      </a>
                    </div>
                    <div className="dtable-wrap">
                      <table className="dtable">
                        <thead>
                          <tr>
                            {rel.columns.map((c) => (
                              <th key={c}>{c}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {rel.rows.slice(0, 12).map((row, i) => (
                            <tr key={i}>
                              {rel.columns.map((c) => (
                                <td
                                  key={c}
                                  className={`cell${row[c] === null ? ' null' : ''}`}
                                  onClick={() =>
                                    store.openDrill({
                                      table: rel.table,
                                      column: c,
                                      value: row[c] === null ? undefined : String(row[c]),
                                    })
                                  }
                                  title="where did this come from?"
                                >
                                  {row[c] === null ? '—' : clip(String(row[c]), 90)}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {rel.rows.length > 12 && (
                        <div className="dtable-more">
                          {rel.rows.length - 12} more rows in <code>{rel.table}</code>
                        </div>
                      )}
                    </div>
                  </section>
                ))}

                <div className="rail-actions">
                  <a className="btn" href={href('studio', { params: { key } })}>
                    Deconstruct this reel
                  </a>
                  <a className="btn" href={href('graph', { params: { keys: key } })}>
                    In the graph
                  </a>
                  <button
                    className="btn-ghost"
                    onClick={() => {
                      void navigator.clipboard?.writeText(key);
                      setSaid('key copied');
                    }}
                  >
                    <Copy size={11} /> {key}
                  </button>
                </div>
              </div>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}

/**
 * The full-width marker track, with the sprite-sheet preview on hover.
 *
 * The preview is the reason a scrub feels instant: the sheet is one JPEG fetched
 * once, so moving the pointer only changes `background-position`. `preview_network`
 * should show **zero** requests while scrubbing — if it shows one per pointer
 * move, something has reintroduced a thumbnail endpoint.
 */
function ScrubTrack({
  moments,
  duration,
  at,
  onSeek,
  spriteKey,
  sprite,
}: {
  moments: Moment[];
  duration: number | null;
  at: number;
  onSeek: (t: number) => void;
  spriteKey: string;
  sprite: SpriteMeta | null;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<{ t: number; x: number } | null>(null);
  const span = duration && duration > 0 ? duration : sprite?.duration || 0;

  const tile = useMemo(() => {
    if (!sprite || !hover || !sprite.count || !sprite.interval) return null;
    const i = Math.min(sprite.count - 1, Math.max(0, Math.round(hover.t / sprite.interval)));
    const col = i % Math.max(1, sprite.cols);
    const row = Math.floor(i / Math.max(1, sprite.cols));
    // Drawn at the frame strip's width rather than the sheet's own tile size.
    // This archive's sheets are 160×284, which would put a 284 px panel over a
    // 26 px track and cover most of the reel you are scrubbing through. Scaling
    // `background-size` and the offset by the same factor is still one fetch and
    // one `background-position` change — the browser does the resampling, so the
    // 16 ms budget is untouched — and it makes the preview the same size as the
    // "no sheet yet" placeholder, so the popup does not resize on you when you
    // move between a derived reel and one the mirror has not reached.
    const k = sprite.tile_w > 0 ? POP_W / sprite.tile_w : 1;
    const h = sprite.tile_h * k;
    return {
      width: POP_W,
      height: h,
      backgroundImage: `url(${spriteUrl(spriteKey)})`,
      // Deliberately unrounded, and expressed in the *same* scaled units as the
      // offsets below: rounding the sheet and the offset separately lets them
      // disagree by a fraction of a pixel, which shows up as a sliver of the
      // neighbouring frame down one edge.
      backgroundSize: `${sprite.cols * POP_W}px ${sprite.rows * h}px`,
      backgroundPosition: `-${col * POP_W}px -${row * h}px`,
    } as React.CSSProperties;
  }, [sprite, hover, spriteKey]);

  return (
    <div
      className="scrub"
      ref={box}
      onMouseMove={(e) => {
        const el = box.current;
        if (!el || span <= 0) return;
        const r = el.getBoundingClientRect();
        const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
        // The popup is centred on the pointer and every ancestor up to
        // `.watch-body` clips, so an unclamped `x` slices the preview in half at
        // both ends of the track — exactly where you scrub to check the first and
        // last second. `half` is capped at r.width / 2 so a track narrower than
        // the popup still centres it instead of inverting the bounds.
        const half = Math.min((POP_W + POP_PAD) / 2, r.width / 2);
        setHover({
          t: frac * span,
          x: Math.min(r.width - half, Math.max(half, frac * r.width)),
        });
      }}
      onMouseLeave={() => setHover(null)}
    >
      <Spectrum
        moments={moments}
        duration={span || null}
        at={at}
        onSeek={onSeek}
        height={26}
        className="spectrum-live"
      />
      {hover && (
        <div className="scrub-pop" style={{ left: hover.x }}>
          {tile ? (
            <div className="scrub-tile" style={tile} />
          ) : (
            <div className="scrub-tile scrub-tile-none">no sprite sheet yet</div>
          )}
          <div className="scrub-t">{fmtT(hover.t)}</div>
        </div>
      )}
    </div>
  );
}

/** The passages, in time order, each one a seek. */
function PassageList({
  moments,
  q,
  at,
  onSeek,
}: {
  moments: Moment[];
  q: string;
  at: number;
  onSeek: (t: number) => void;
}) {
  const ordered = useMemo(
    () =>
      [...moments].sort((a, b) => {
        // Whole-reel claims last: they are true of every second, so putting them
        // at 0:00 would push the first real moment off the top of the list.
        if (a.t_start === null && b.t_start === null) return 0;
        if (a.t_start === null) return 1;
        if (b.t_start === null) return -1;
        return a.t_start - b.t_start;
      }),
    [moments]
  );

  if (!ordered.length) {
    return (
      <div className="state-box">
        <div className="head">No claims on record</div>
        <div>Nothing has watched, transcribed or read this reel yet.</div>
      </div>
    );
  }

  return (
    <ul className="passages">
      {ordered.map((m) => {
        const live =
          m.t_start !== null && at >= m.t_start && at <= (m.t_end ?? m.t_start + 3);
        return (
          <li key={m.id} className={live ? 'is-live' : undefined}>
            <button
              className="passage"
              onClick={() => (m.t_start !== null ? onSeek(m.t_start) : undefined)}
              disabled={m.t_start === null}
              title={m.t_start === null ? 'this claim is about the whole reel' : `seek to ${fmtT(m.t_start)}`}
            >
              <span className={chipClass(m.source || m.src_table)}>
                {m.source || m.src_table || 'meta'}
              </span>
              <span className="passage-t">{m.t_start === null ? 'whole' : fmtT(m.t_start)}</span>
              <span className="passage-text">
                <Mark text={m.text} q={q} />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

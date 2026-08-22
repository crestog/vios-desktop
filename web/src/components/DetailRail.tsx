/**
 * components/DetailRail.tsx — one reel's facts, beside the grid.
 *
 * The point of the rail is to answer "is this the one?" without leaving the
 * grid — so it shows what a card cannot fit and stops there. Everything deeper
 * (the frame track, the full evidence list, the drill-down) lives in the player
 * route, which is one click away and linkable.
 *
 * It asks for `full=false`. The full payload carries every related row in the
 * database for that key, which for a well-covered reel is hundreds of rows —
 * fine for the player, wasteful for a rail that shows eight facts and closes.
 *
 * `playback.where` is reported rather than hidden, because it is the difference
 * between instant playback and a Telegram download: `local` means the mirror has
 * it, `remote` means the channel does and this disk does not. The rail offers to
 * jump the queue in that case, which is the one action that changes the answer.
 */

import { ArrowDownToLine, ExternalLink, Copy, Play, X, Zap } from 'lucide-react';
import type { Moment } from '../types';
import { enqueueVideo, getVideo, posterUrl, prioritizeMirror } from '../lib/api';
import { channelTally, chipClass } from '../lib/channels';
import { fmtAgo, fmtBytes, fmtCompact, fmtDate, fmtDur, fmtT, clip } from '../lib/format';
import { href } from '../lib/router';
import { useFetch } from '../lib/useFetch';
import { useState } from 'react';

export interface DetailRailProps {
  videoKey: string;
  q?: string;
  onClose: () => void;
  hint?: string;
}

const WHERE_NOTE: Record<string, string> = {
  local: 'the file is on this disk — playback is instant',
  cache: 'downloaded from the channel and kept on this disk',
  remote: 'in the channel, not on this disk yet — it will stream while it downloads',
};

export default function DetailRail({ videoKey, q, onClose, hint }: DetailRailProps) {
  const v = useFetch((signal) => getVideo(videoKey, false, signal), [videoKey]);
  const [said, setSaid] = useState<string | null>(null);

  const meta = v.data?.meta;
  const moments: Moment[] = v.data?.moments || [];
  const channels = channelTally(
    moments.reduce<Record<string, number>>((acc, m) => {
      const k = m.source || m.src_table || 'meta';
      acc[k] = (acc[k] || 0) + 1;
      return acc;
    }, {})
  );
  const where = v.data?.playback?.where;

  return (
    <aside className="rail rail-right" aria-label="Reel details">
      <div className="rail-head">
        <span className="view-title">Details</span>
        <span className="spacer" />
        <button className="btn-ghost" onClick={onClose} title="close the rail">
          <X size={12} />
        </button>
      </div>

      {v.error ? (
        <div className="state-box err">
          <div className="head">Could not read that reel</div>
          <div>{v.error}</div>
        </div>
      ) : v.first && v.loading ? (
        <div className="rail-body">
          <div className="skel" style={{ aspectRatio: '9 / 16', width: '100%' }} />
          <div className="skel" style={{ height: 14, marginTop: 10 }} />
          <div className="skel" style={{ height: 14, marginTop: 6, width: '60%' }} />
        </div>
      ) : (
        <div className="rail-body">
          <a
            className="rail-poster"
            href={href('watch', { key: videoKey, params: { q } })}
            title="open the player"
          >
            <img src={posterUrl(videoKey, 360)} alt="" draggable={false} />
            <span className="rail-play">
              <Play size={16} />
            </span>
          </a>

          <h3 className="rail-title">{meta?.title || videoKey}</h3>

          <dl className="kv">
            {meta?.creator && (
              <>
                <dt>creator</dt>
                <dd>
                  <a href={href('library', { params: { creator: meta.creator } })}>{meta.creator}</a>
                </dd>
              </>
            )}
            {meta?.category && (
              <>
                <dt>category</dt>
                <dd>
                  <a href={href('library', { params: { category: meta.category } })}>
                    {meta.category}
                  </a>
                </dd>
              </>
            )}
            <dt>length</dt>
            <dd>{fmtDur(meta?.duration ?? null)}</dd>
            {typeof meta?.likes === 'number' && (
              <>
                <dt>likes</dt>
                <dd>{fmtCompact(meta.likes)}</dd>
              </>
            )}
            {meta?.width && meta?.height && (
              <>
                <dt>frame</dt>
                <dd>
                  {meta.width}×{meta.height}
                </dd>
              </>
            )}
            {meta?.created_at && (
              <>
                <dt>added</dt>
                <dd title={fmtDate(meta.created_at)}>{fmtAgo(meta.created_at)}</dd>
              </>
            )}
            <dt>claims</dt>
            <dd>{fmtCompact(moments.length || meta?.moment_count || 0)}</dd>
            {where && (
              <>
                <dt>file</dt>
                <dd title={WHERE_NOTE[where] || where}>
                  <span className={`sb-seg ${where === 'remote' ? 'sb-warn' : 'sb-ok'}`}>{where}</span>
                  {v.data?.playback?.size ? ` · ${fmtBytes(v.data.playback.size)}` : ''}
                </dd>
              </>
            )}
          </dl>

          {channels.length > 0 && (
            <div className="card-chips rail-chips">
              {channels.map(({ channel, count }) => (
                <span key={channel} className={chipClass(channel)} title={`${count} claims`}>
                  {channel} <span className="facet-v-n">{count}</span>
                </span>
              ))}
            </div>
          )}

          {meta?.caption && <p className="rail-caption">{clip(meta.caption, 320)}</p>}

          {moments.length > 0 && (
            <>
              <div className="section-h">First passages</div>
              <ul className="rail-moments">
                {moments.slice(0, 6).map((m) => (
                  <li key={m.id}>
                    <a
                      href={href('watch', {
                        key: videoKey,
                        params: { t: m.t_start !== null ? m.t_start.toFixed(2) : undefined, q },
                      })}
                    >
                      <span className={chipClass(m.source || m.src_table)}>
                        {m.source || m.src_table || 'meta'}
                      </span>
                      <span className="passage-t">{m.t_start === null ? 'whole reel' : fmtT(m.t_start)}</span>
                      <span className="passage-text">{clip(m.text, 120)}</span>
                    </a>
                  </li>
                ))}
              </ul>
              {moments.length > 6 && (
                <a className="rail-more" href={href('watch', { key: videoKey, params: { q } })}>
                  all {fmtCompact(moments.length)} passages in the player{' '}
                  <ExternalLink size={11} />
                </a>
              )}
            </>
          )}

          <div className="rail-actions">
            <a className="btn" href={href('watch', { key: videoKey, params: { q } })}>
              <Play size={12} /> Open player
            </a>
            {where === 'remote' && (
              <button
                className="btn"
                onClick={async () => {
                  try {
                    await prioritizeMirror(videoKey);
                    setSaid('moved to the front of the download queue');
                  } catch (e) {
                    setSaid(String((e as Error).message || e));
                  }
                }}
              >
                <ArrowDownToLine size={12} /> Download now
              </button>
            )}
            <button
              className="btn"
              onClick={async () => {
                try {
                  const r = await enqueueVideo(videoKey);
                  setSaid(`queued ${r.enqueued} pass${r.enqueued === 1 ? '' : 'es'} on this machine`);
                } catch (e) {
                  setSaid(String((e as Error).message || e));
                }
              }}
              title="run the local passes this reel is missing"
            >
              <Zap size={12} /> Process here
            </button>
            <a className="btn" href={href('studio', { params: { key: videoKey } })}>
              Deconstruct
            </a>
            <button
              className="btn-ghost"
              onClick={() => {
                void navigator.clipboard?.writeText(videoKey);
                setSaid('key copied');
              }}
            >
              <Copy size={11} /> {videoKey}
            </button>
          </div>

          {said && <div className="rail-said">{said}</div>}
          {hint && <div className="rail-hint">{hint}</div>}
        </div>
      )}
    </aside>
  );
}

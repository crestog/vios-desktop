/**
 * components/StatusBar.tsx — "what does the system know right now", always visible.
 *
 * Every number here is measured. The v1 status strip had `'68.4'` GB and
 * `'RTX 3050 Laptop (4.9 GB VRAM usable)'` written into the markup as fallbacks,
 * which is worse than showing nothing: it looked right on the machine it was
 * typed on and lied everywhere else, including on the same machine after a disk
 * filled up. So a fact that has not arrived renders as `—`, and the tooltip says
 * what would have filled it.
 *
 * It reads the store rather than fetching. Two poll rates already run there
 * (3 s for mirror and queue, 60 s for hardware), and a strip that fetched its
 * own copy would double the request rate to show the same values.
 */

import { useDisk, useEngine, useHost, useMirror, useOffline } from '../lib/store';
import { count, fmtBytes, fmtCount, fmtPct, GB } from '../lib/format';
import { href } from '../lib/router';
import { useFetch } from '../lib/useFetch';
import { getStatus } from '../lib/api';

export default function StatusBar() {
  const mirror = useMirror();
  const engine = useEngine();
  const host = useHost();
  const disk = useDisk();
  const offline = useOffline();

  // The archive counts do not move unless a shard lands, so this is fetched
  // once rather than polled. Home is where live counts belong.
  // `.search` because `/api/status` is an envelope — see `StatusEnvelope`.
  const { data: status } = useFetch(getStatus, []);
  const archive = status?.search;

  const gpu = host?.gpus?.[0];
  const freeGb = disk ? disk.free_bytes / GB : null;
  const lowDisk = disk?.below_floor || (freeGb !== null && freeGb < (disk?.free_floor_gb ?? 8));

  // torch reports the full marketing name — "NVIDIA GeForce RTX 3050 6GB Laptop
  // GPU" — which is 38 characters of which 15 are the vendor saying its own name
  // twice. Dropped here rather than truncated by CSS, so what is lost is the
  // part that carries no information. The tooltip keeps the string verbatim.
  const gpuName = gpu?.name?.replace(/^NVIDIA\s+(GeForce\s+)?|^AMD\s+|^Intel\(R\)\s+/i, '');

  return (
    <footer className="vios-status-bar">
      <div className="sb-left">
        {offline ? (
          <span className="sb-seg sb-bad" title="the Python side stopped answering — is the window's server still running?">
            ● {offline}
          </span>
        ) : (
          <span className="sb-seg sb-ok" title="the local server is answering">
            ● local
          </span>
        )}

        <a
          className="sb-seg"
          href={href('library')}
          title={
            archive
              ? `${count(archive.moments as number, 'claim')} about ${count(
                  archive.videos as number,
                  'reel'
                )}`
              : 'reading /api/status'
          }
        >
          {count(archive?.videos as number, 'reel')} · {count(archive?.moments as number, 'claim')}
        </a>

        {archive?.dense_ready !== undefined && (
          <span
            className="sb-seg sb-opt"
            title={
              archive.dense_ready
                ? `semantic search is live${archive.dense_model ? ` — ${archive.dense_model}` : ''}`
                : 'semantic search is not built yet — keyword search still works'
            }
          >
            {archive.dense_ready ? 'semantic ✓' : 'semantic —'}
          </span>
        )}
      </div>

      <div className="sb-right">
        {mirror && (
          <a
            className="sb-seg"
            href={href('admin')}
            title={
              mirror.running
                ? `mirroring: ${mirror.active_downloads.length} downloading, ${mirror.active_derives.length} deriving`
                : mirror.paused
                  ? 'the mirror is paused'
                  : 'the mirror is idle'
            }
          >
            mirror {mirror.running ? '◐' : mirror.paused ? '❙❙' : '○'} {fmtCount(mirror.downloaded)}/
            {fmtCount(mirror.total_videos)}
            {mirror.total_videos > 0 && (
              <span className="sb-dim"> {fmtPct(mirror.downloaded, mirror.total_videos)}</span>
            )}
          </a>
        )}

        {engine && (engine.pending > 0 || engine.running > 0 || engine.running_worker) && (
          <a
            className="sb-seg"
            href={href('engine')}
            title={`${engine.pending} pending, ${engine.running} running, ${engine.completed} done, ${engine.failed} failed`}
          >
            engine {engine.paused ? '❙❙' : engine.running ? '◐' : '○'} {fmtCount(engine.pending)}
          </a>
        )}

        <a
          className={`sb-seg${lowDisk ? ' sb-warn' : ''}`}
          href={href('admin')}
          title={
            disk
              ? `${fmtBytes(disk.free_bytes)} free · videos ${fmtBytes(
                  disk.video_bytes
                )} · proxies ${fmtBytes(disk.proxy_bytes)} · derived ${fmtBytes(
                  disk.derived_bytes
                )} · models ${fmtBytes(disk.model_bytes)} · db ${fmtBytes(disk.db_bytes)}`
              : 'reading /api/desktop/disk'
          }
        >
          {disk ? `${fmtBytes(disk.free_bytes)} free` : '— free'}
        </a>

        <span
          className="sb-seg sb-gpu"
          title={
            gpu
              ? `${gpu.name} · ${fmtCount(gpu.free_mb)} MB free of ${fmtCount(
                  gpu.total_mb
                )} MB · usable for a model: ${fmtCount(host?.usable_vram_mb)} MB${
                  host?.compute_capability
                    ? ` · sm_${host.compute_capability}${host.dtype ? ` (${host.dtype})` : ''}`
                    : ''
                }`
              : host
                ? host.note || 'no GPU visible to torch on this machine'
                : 'probing the machine'
          }
        >
          {gpu ? `${gpuName} ${fmtCount(gpu.free_mb)}MB` : host ? 'no gpu' : '—'}
        </span>
      </div>
    </footer>
  );
}

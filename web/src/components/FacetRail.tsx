/**
 * components/FacetRail.tsx — narrowing, with the counts visible.
 *
 * The rail exists because of a specific v1 failure: filters were a row of
 * dropdowns with no counts, so choosing one was a guess and an empty result
 * afterwards was indistinguishable from a broken query. Here every value shows
 * how many rows it would leave, and the counts come from the current *result
 * set* for creator and category — so the rail answers "who talks about this"
 * as a side effect of narrowing.
 *
 * Two rules that are easy to get wrong and matter:
 *
 *   - **Clicking the active value clears it.** A radio group with no "all"
 *     option traps you: once a creator is picked there is no way back except
 *     editing the URL. Toggling is one line and removes the trap.
 *
 *   - **Channels are counted archive-wide, not per result.** The caller passes
 *     `/api/facets`'s totals rather than the result set's, because a source
 *     filter built from the current results can only ever narrow further, never
 *     widen — the one thing you want it for.
 *
 *   - **A collection count is filings, not videos.** One reel filed on two
 *     shelves is counted by both, so the numbers in that group sum past the
 *     archive's video count. That is the many-to-one relationship being honest
 *     rather than a duplicate: picking either shelf still returns the reel once.
 */

import { useState } from 'react';
import { ChevronDown, RotateCcw } from 'lucide-react';
import type { Facet } from '../types';
import { CHANNEL_MEANING, chipClass, type ChannelName } from '../lib/channels';
import { fmtCount } from '../lib/format';

const SHOWN = 8;

/** Duration buckets, in seconds. Reels cluster hard under a minute. */
const DURATIONS: Array<{ label: string; min?: number; max?: number }> = [
  { label: 'under 15s', max: 15 },
  { label: '15 – 30s', min: 15, max: 30 },
  { label: '30 – 60s', min: 30, max: 60 },
  { label: 'over a minute', min: 60 },
];

export interface FacetRailProps {
  creators?: Facet[];
  categories?: Facet[];
  /** Saved collections. Counts are filings, so they sum past the video count. */
  collections?: Facet[];
  channels?: Array<{ channel: ChannelName; count: number }>;
  active: { creator: string; category: string; source: string; collection?: string };
  minDur?: number;
  maxDur?: number;
  minHits?: number;
  /** Given a patch of URL params. `''` clears a param; the caller owns the URL. */
  onChange: (patch: Record<string, unknown>) => void;
}

function Group({
  title,
  count,
  children,
  open: initial = true,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
  open?: boolean;
}) {
  const [open, setOpen] = useState(initial);
  return (
    <section className={`facet${open ? ' is-open' : ''}`}>
      <button className="facet-h" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <ChevronDown size={12} className="facet-caret" />
        <span>{title}</span>
        {count !== undefined && <span className="facet-n">{fmtCount(count)}</span>}
      </button>
      {open && <div className="facet-body">{children}</div>}
    </section>
  );
}

function ValueList({
  values,
  active,
  onPick,
}: {
  values: Facet[];
  active: string;
  onPick: (v: string) => void;
}) {
  const [all, setAll] = useState(false);
  if (!values.length) return <div className="facet-empty">nothing to narrow by yet</div>;
  // The active value is pinned into view even when it falls outside the top
  // eight — otherwise a filter can be on with no visible way to switch it off.
  const head = all ? values : values.slice(0, SHOWN);
  const shown =
    active && !head.some((v) => v.value === active)
      ? [...head, values.find((v) => v.value === active) || { value: active, count: 0 }]
      : head;
  return (
    <>
      <ul className="facet-list">
        {shown.map((f) => (
          <li key={f.value}>
            <button
              className={`facet-v${f.value === active ? ' on' : ''}`}
              onClick={() => onPick(f.value === active ? '' : f.value)}
              title={f.value === active ? 'clear this filter' : `only ${f.value}`}
            >
              <span className="facet-v-label">{f.value || '(unattributed)'}</span>
              <span className="facet-v-n">{fmtCount(f.count)}</span>
            </button>
          </li>
        ))}
      </ul>
      {values.length > SHOWN && (
        <button className="facet-more" onClick={() => setAll((v) => !v)}>
          {all ? 'show fewer' : `show all ${fmtCount(values.length)}`}
        </button>
      )}
    </>
  );
}

export default function FacetRail({
  creators,
  categories,
  collections,
  channels,
  active,
  minDur,
  maxDur,
  minHits,
  onChange,
}: FacetRailProps) {
  const any =
    Boolean(active.creator || active.category || active.source || active.collection) ||
    minDur !== undefined ||
    maxDur !== undefined ||
    (minHits ?? 0) > 1;

  return (
    <aside className="rail" aria-label="Filters">
      <div className="rail-head">
        <span className="view-title">Narrow</span>
        <span className="spacer" />
        {any && (
          <button
            className="btn-ghost"
            onClick={() =>
              onChange({
                creator: '',
                category: '',
                source: '',
                collection: '',
                min_dur: '',
                max_dur: '',
                min_hits: '',
              })
            }
            title="clear every filter"
          >
            <RotateCcw size={11} /> clear
          </button>
        )}
      </div>

      {/* First, because it is the only filter the person built themselves. The
          count is filings rather than videos, which is why these add up past the
          archive's video count — one reel on two shelves is counted by both, and
          picking either one still finds it exactly once. */}
      <Group title="Collection" count={collections?.length}>
        <ValueList
          values={collections || []}
          active={active.collection || ''}
          onPick={(v) => onChange({ collection: v })}
        />
      </Group>

      <Group title="Creator" count={creators?.length}>
        <ValueList
          values={creators || []}
          active={active.creator}
          onPick={(v) => onChange({ creator: v })}
        />
      </Group>

      <Group title="Category" count={categories?.length}>
        <ValueList
          values={categories || []}
          active={active.category}
          onPick={(v) => onChange({ category: v })}
        />
      </Group>

      <Group title="Evidence channel" count={channels?.length}>
        {channels && channels.length ? (
          <ul className="facet-chips">
            {channels.map(({ channel, count }) => (
              <li key={channel}>
                <button
                  className={`${chipClass(channel)}${active.source === channel ? ' on' : ''}`}
                  onClick={() => onChange({ source: active.source === channel ? '' : channel })}
                  title={CHANNEL_MEANING[channel]}
                >
                  {channel}
                  <span className="facet-v-n">{fmtCount(count)}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="facet-empty">no channels recorded yet</div>
        )}
      </Group>

      <Group title="Length" open={false}>
        <ul className="facet-list">
          {DURATIONS.map((d) => {
            const on = minDur === d.min && maxDur === d.max;
            return (
              <li key={d.label}>
                <button
                  className={`facet-v${on ? ' on' : ''}`}
                  onClick={() =>
                    onChange(on ? { min_dur: '', max_dur: '' } : { min_dur: d.min ?? '', max_dur: d.max ?? '' })
                  }
                >
                  <span className="facet-v-label">{d.label}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </Group>

      <Group title="Match strength" open={false}>
        <label className="facet-num">
          <span>at least</span>
          <input
            className="input-text"
            type="number"
            min={1}
            max={99}
            value={minHits ?? ''}
            placeholder="1"
            onChange={(e) => onChange({ min_hits: e.target.value })}
          />
          <span>passages in one reel</span>
        </label>
        <p className="facet-note">
          A reel with nine matching lines is usually *about* the thing; one with a single
          match may only mention it.
        </p>
      </Group>
    </aside>
  );
}

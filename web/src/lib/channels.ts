/**
 * lib/channels.ts — the one place a `source` string becomes a colour.
 *
 * The design rule is absolute: **colour in this application means exactly one
 * thing.** A hue identifies which observer produced a claim, and nothing else.
 * So the mapping from the database's `source` strings to the seven channel hues
 * has to live in one file, or the rule quietly stops being true the third time
 * a view guesses.
 *
 * The database's vocabulary is wider than the design's. `atlas/reflect.py`
 * holds:
 *
 *     _KNOWN_SOURCES = {narrative, speech, visual, ocr, caption,
 *                       meta, audio, concept, style}
 *
 * — nine, against the design's seven. And `source_label()` **falls back to
 * `"meta"`** for any content column whose source it cannot name, which makes
 * `meta` the most common label in the archive rather than a rare one.
 *
 * Two decisions follow, and both are deliberate rather than convenient:
 *
 *   - **`meta` does not borrow a channel hue.** It is not an observer: it is
 *     what Instagram declared — a title, a category, a creator handle. Painting
 *     it red would make it indistinguishable from `caption`, which *is* one of
 *     the seven and means something narrower. It gets slate, which recedes,
 *     because "the platform said so" is the weakest evidence in the archive.
 *
 *   - **`audio` does not borrow speech's sky blue.** A music cue and a spoken
 *     sentence are different observations from different models, and the entire
 *     point of the palette is that two different things never share a colour.
 *     It gets orange, which is unused by the seven.
 *
 * Anything else — a source a future pass invents — resolves to `meta` and is
 * labelled with its own literal string, so an unrecognised observer shows up as
 * grey-and-named rather than silently wearing someone else's hue.
 */

import type { ChannelKind, Moment } from '../types';
import { CHANNELS } from '../types';

/** The seven design channels plus the two the database has and the design did not name. */
export type ChannelName = ChannelKind | 'audio' | 'meta';

export const ALL_CHANNELS: ChannelName[] = [...CHANNELS, 'audio', 'meta'];

const KNOWN = new Set<ChannelName>(ALL_CHANNELS);

/** CSS custom property per channel. `--ch-audio` / `--ch-meta` are extensions. */
export const CHANNEL_VAR: Record<ChannelName, string> = {
  speech: 'var(--ch-speech)',
  ocr: 'var(--ch-ocr)',
  visual: 'var(--ch-visual)',
  narrative: 'var(--ch-narrative)',
  style: 'var(--ch-style)',
  caption: 'var(--ch-caption)',
  concept: 'var(--ch-concept)',
  audio: 'var(--ch-audio)',
  meta: 'var(--ch-meta)',
};

/** What each channel actually means, for a tooltip and for the legend. */
export const CHANNEL_MEANING: Record<ChannelName, string> = {
  speech: 'spoken words, transcribed with timings',
  ocr: 'text that appears on screen',
  visual: 'objects, actions and detections in frames',
  narrative: 'a vision-language model describing what happens',
  style: 'lighting, camera motion, pacing, aesthetic',
  caption: "the creator's own caption and hashtags",
  concept: 'entities and taxonomy from the knowledge graph',
  audio: 'non-speech audio — music, effects, laughter',
  meta: 'declared by the platform, not observed by a model',
};

/**
 * Resolve one `source` (or `src_table`) string to a channel.
 *
 * Prefix-tolerant on purpose: a table named `speech_en` or a source written
 * `visual_detect` is that channel, and refusing to see it would paint a real
 * observation grey over a naming convention.
 */
export function channelOf(source: string | null | undefined): ChannelName {
  const s = String(source || '').trim().toLowerCase();
  if (!s) return 'meta';
  if (KNOWN.has(s as ChannelName)) return s as ChannelName;
  for (const c of ALL_CHANNELS) {
    if (s.startsWith(c) || s.includes(`_${c}`)) return c;
  }
  return 'meta';
}

export const channelVar = (source: string | null | undefined): string =>
  CHANNEL_VAR[channelOf(source)];

/** The chip class in `main.css`. One per channel, label included in the markup. */
export const chipClass = (source: string | null | undefined): string =>
  `chip-channel chip-${channelOf(source)}`;

/**
 * Which channels a set of moments actually used, in palette order.
 *
 * Palette order rather than first-seen order, so the same video shows the same
 * chip sequence every time it is rendered — a card whose chips reshuffle
 * between a search and the library reads as two different videos.
 */
export function channelsIn(moments: Moment[] | undefined): ChannelName[] {
  if (!moments || !moments.length) return [];
  const seen = new Set<ChannelName>();
  for (const m of moments) seen.add(channelOf(m.source || m.src_table));
  return ALL_CHANNELS.filter((c) => seen.has(c));
}

/** Counts per channel — what the Home page's channel bar and facets need. */
export function channelTally(
  sources: Record<string, number> | string | null | undefined
): Array<{ channel: ChannelName; count: number }> {
  const out = new Map<ChannelName, number>();
  if (sources && typeof sources === 'object') {
    for (const [k, v] of Object.entries(sources)) {
      const c = channelOf(k);
      out.set(c, (out.get(c) || 0) + (Number(v) || 0));
    }
  } else if (typeof sources === 'string' && sources) {
    // `/api/library` can hand back a comma-joined string rather than a map.
    for (const part of sources.split(/[,\s]+/)) {
      if (!part) continue;
      const c = channelOf(part);
      out.set(c, (out.get(c) || 0) + 1);
    }
  }
  return ALL_CHANNELS.filter((c) => out.has(c)).map((c) => ({
    channel: c,
    count: out.get(c) || 0,
  }));
}

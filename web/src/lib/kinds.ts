/**
 * lib/kinds.ts — the graph's node kinds, and the one place they become a colour.
 *
 * These four kinds are not a guess. `atlas/graph.py:rebuild()` writes exactly
 * `video`, `dim`, `tag` and `hashtag` into `graph_nodes`, and puts something
 * different in `sub` for each one:
 *
 *   | kind     | `sub` holds            | what it is                            |
 *   |----------|------------------------|---------------------------------------|
 *   | video    | `"videos"`             | a reel                                |
 *   | dim      | the dimension *table*  | a row a reel points at by foreign key |
 *   | tag      | the *column* it came from | a value repeated across ≥2 reels   |
 *   | hashtag  | `"hashtags"`           | a `#word` mined out of the text       |
 *
 * `lib/channels.ts` owns the rule that a hue identifies **which observer made a
 * claim**, and that rule decides the palette here rather than aesthetics:
 *
 *   - A **tag** node carries `meta.source` — `reflect.source_label(table,
 *     column)`, the same string the search results colour by. So a tag mined
 *     from the transcript is speech-blue here *and* in the drawer, which is the
 *     whole point of having a palette at all.
 *   - A **video** is not a claim, it is the thing claims are about, so it gets an
 *     ink tone and never a channel hue.
 *   - A **dim** row is platform-declared structure, not an observation.
 *   - A **hashtag** is mined from whichever text column happened to contain a
 *     `#`, and the node keeps no record of which. That could be a caption or it
 *     could be OCR of a watermark, so it gets a neutral tone: guessing `caption`
 *     would be a colour asserting a provenance nobody checked.
 *
 * The `--k-*` tones are deliberately desaturated so none of them can be mistaken
 * for one of the seven channels.
 *
 * `resolveColor` exists because the graph draws on a canvas, and a canvas cannot
 * be handed `var(--k-video)`. It reads the computed value off the root element,
 * which keeps the palette defined in CSS — one source of truth — instead of
 * duplicating hex codes into TypeScript.
 */

import { ALL_CHANNELS, CHANNEL_MEANING, channelOf, type ChannelName } from './channels';

const CHANNEL_SET = new Set<string>(ALL_CHANNELS);

/** The shape every graph node has, and all this module needs of it. */
export interface KindedNode {
  kind: string;
  sub?: string | null;
  meta?: Record<string, unknown>;
}

/** The CSS custom property that owns this node's colour. */
export function nodeProp(n: KindedNode): string {
  const kind = String(n.kind || '').toLowerCase();

  if (kind === 'tag') {
    const src = n.meta?.source;
    // Only claim a channel when the node actually records one.
    if (typeof src === 'string' && src) return `--ch-${channelOf(src)}`;
    return '--k-tag';
  }
  if (kind === 'video') return '--k-video';
  if (kind === 'hashtag') return '--k-tag';
  if (kind === 'dim') return '--k-dim';
  if (kind === 'table' || kind === 'anchor') return '--k-other';

  // A kind a future pass invents: honour it if it names a channel, otherwise
  // grey. Same fallback discipline as `channelOf`.
  if (CHANNEL_SET.has(kind)) return `--ch-${kind}`;
  return '--k-other';
}

/** For inline styles: `var(--k-video)`. */
export const nodeCss = (n: KindedNode): string => `var(${nodeProp(n)})`;

/**
 * What to call this node's type in the interface.
 *
 * `dim` and `tag` are implementation words, and the useful label is in `sub`:
 * a dim row from the `creators` table is a "creator", and a tag mined from the
 * `objects` column is an "objects" value. Singularised crudely — trailing `s`
 * off a table name is right far more often than it is wrong, and it only ever
 * affects a caption.
 */
export function nodeTypeLabel(n: KindedNode): string {
  const kind = String(n.kind || '').toLowerCase();
  const sub = String(n.sub || '').trim();
  if (kind === 'video') return 'reel';
  if (kind === 'hashtag') return 'hashtag';
  if (kind === 'dim') return sub ? sub.replace(/s$/, '') : 'row';
  if (kind === 'tag') return sub || 'tag';
  return kind || 'node';
}

/** What this node is, in one phrase — for a legend and a tooltip. */
export function nodeNote(n: KindedNode): string {
  const kind = String(n.kind || '').toLowerCase();
  const sub = String(n.sub || '').trim();
  if (kind === 'video') return 'a reel';
  if (kind === 'dim')
    return `a row in ${sub || 'a dimension table'} that reels point at by foreign key`;
  if (kind === 'tag') {
    const src = n.meta?.source;
    const observer =
      typeof src === 'string' && src ? ` — ${CHANNEL_MEANING[channelOf(src)]}` : '';
    return `a value that repeats across reels in ${sub || 'a text column'}${observer}`;
  }
  if (kind === 'hashtag') return 'a #hashtag found in the text';
  if (CHANNEL_SET.has(kind)) return CHANNEL_MEANING[kind as ChannelName];
  return 'a node the graph builder produced';
}

/**
 * Resolve a custom property to something a canvas can fill with.
 *
 * Returns an empty string when the property is not defined, so the caller can
 * fall back rather than drawing invisible nodes — an undefined var handed to
 * `fillStyle` is silently ignored and leaves the *previous* colour in place,
 * which is the worst possible failure in a view where colour carries meaning.
 */
export function resolveColor(prop: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(prop).trim();
}

const cache = new Map<string, string>();

/**
 * The same, memoised — for the graph, which needs a colour per node per rebuild
 * and a handful more per frame.
 *
 * `getComputedStyle` is a style read: four hundred of them while building a
 * layout is four hundred chances to force a recalculation. The palette is
 * defined once in `main.css` and there is no theme switch in this application,
 * so caching is safe; `clearColorCache()` exists for the day there is one.
 */
export function color(prop: string): string {
  const had = cache.get(prop);
  if (had !== undefined) return had;
  const v = resolveColor(prop);
  cache.set(prop, v);
  return v;
}

export function clearColorCache(): void {
  cache.clear();
}

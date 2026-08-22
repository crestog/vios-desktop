/**
 * components/Mark.tsx — the query, highlighted in the text it matched.
 *
 * FTS5 has `snippet()`, and Atlas does not use it for these passages: the rows
 * come back as whole `text` columns, so highlighting happens here. That is fine
 * and arguably better — the same component highlights a caption, a transcript
 * line and an OCR string, and there is no server round trip to re-highlight
 * when the query narrows.
 *
 * Two rules it follows, both because getting them wrong is worse than not
 * highlighting at all:
 *
 *   - **Every term is escaped before it becomes a regex.** A query of `c++` or
 *     `(hook)` is a perfectly reasonable thing to search for and would throw on
 *     an unescaped `RegExp`, taking the whole result list down with it.
 *   - **FTS operators are stripped, not matched.** `hook OR line` looks for two
 *     words; painting the literal string "OR" yellow would be a lie about what
 *     matched.
 */

import { useMemo } from 'react';

const OPERATORS = new Set(['and', 'or', 'not', 'near']);

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Tokens worth highlighting: two characters or more, no bare FTS operators. */
export function terms(q: string | null | undefined): string[] {
  return String(q || '')
    .replace(/["*^:]/g, ' ')
    .split(/[\s,;/()]+/)
    .map((t) => t.trim())
    .filter((t) => t.length >= 2 && !OPERATORS.has(t.toLowerCase()));
}

export default function Mark({ text, q }: { text: string; q?: string | null }) {
  const parts = useMemo(() => {
    const list = terms(q);
    if (!list.length || !text) return null;
    let re: RegExp;
    try {
      re = new RegExp(`(${list.map(escapeRe).join('|')})`, 'gi');
    } catch {
      return null;
    }
    return text.split(re);
  }, [text, q]);

  if (!parts) return <>{text}</>;

  const wanted = new Set(terms(q).map((t) => t.toLowerCase()));
  return (
    <>
      {parts.map((p, i) =>
        wanted.has(p.toLowerCase()) ? (
          <mark key={i} className="mk">
            {p}
          </mark>
        ) : (
          <span key={i}>{p}</span>
        )
      )}
    </>
  );
}

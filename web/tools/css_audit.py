"""css_audit.py — every class the interface asks for, against every class the
stylesheet answers.

This exists because a missing rule is invisible in every check that matters:
TypeScript compiles, the bundle builds, the server returns 200, and the view
renders as a stack of unstyled divs. Nothing fails. The only way to see it is to
compare the two lists, and the only way to keep seeing it is to make the
comparison cheap enough to re-run after every batch.

Run from web/:  python tools/css_audit.py [--all]

Without --all it prints only the groups that are still missing, largest first,
which is the work-list. With --all it also prints defined-but-unused, which is
advisory: a class can be legitimately defined ahead of the markup, and several
are matched dynamically in ways this script cannot see.

How it reads the source: it finds each `className=` / `cls:` and takes the
*whole* expression by matching braces, then pulls every string literal out of
that expression and splits it on whitespace. Regexing the shapes individually
was the first attempt and it silently missed the nested case —

    className={`spectrum${live ? ' spectrum-live' : ''}${cls ? ` ${cls}` : ''}`}

— where a `[^`]*` capture stops at the inner backtick, so `.spectrum` itself
never appeared in the used set and its absence from the stylesheet went
unreported. An audit with a blind spot is worse than no audit, because it is
believed.

Known blind spots that remain, stated so a future reader does not chase them:

  - Template interpolation. `` `cap-pill-${state}` `` yields the fragment
    `cap-pill-` after ${...} is stripped. Fragments ending in `-` are dropped
    rather than reported, because the real class names are constructed at
    runtime and cannot be enumerated from the source.
  - Descendant and compound selectors. `.rail-head .btn-icon` defines
    `rail-head` and `btn-icon`; `.cap-stat.cs-failed .n` defines three. Every
    class token in a selector counts as defined, which is right for coverage
    (the rule exists) and wrong for "is this class useful on its own" (it is
    not) — so a class that only ever appears as a descendant qualifier will
    read as defined here. That is the correct trade for this script's job.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
SRC = WEB / "src"

# Where a class list starts. The table-driven `cls:` / `className:` object
# literals this app uses for state maps count as much as the JSX attribute.
STARTS = re.compile(r"""(?:className\s*=|\bcls\s*:|\bclassName\s*:)\s*""")

# Every string literal inside an expression. Searched over the whole expression
# without regard to nesting, which is deliberate: the class in
# `` `sp-band${b.whole ? ' sp-whole' : ''}` `` lives *inside* an interpolation,
# and a scanner that only read the top level would drop it. The cost is that an
# argument which is not a class — `chipClass('meta')` — is read as one; a
# handful of harmless false positives is the right side of that trade.
QUOTED = re.compile(r"'([^'\\]*)'|\"([^\"\\]*)\"")
TEMPLATE = re.compile(r"`([^`\\]*)`")

# `x === 'frames' ? …` — the operand of a comparison is a value, not a class,
# and leaving it in was the largest source of false positives.
COMPARE = re.compile(r"[=!]==?\s*(?:'[^']*'|\"[^\"]*\"|`[^`]*`)")

INTERP = re.compile(r"\$\{[^{}]*\}")

# What CSS will actually accept as a class name. The guard that keeps `${className`,
# `''}`, `?` and `:` — the debris of a nested template the regex cut in half —
# out of a list whose whole value is that every line on it is real work.
IDENT = re.compile(r"^-?[_a-zA-Z][\w-]*$")


def _expr_at(text: str, i: int) -> tuple[str, int]:
    """The class expression starting at `i`, and where it ends.

    A bare quoted string is returned *with* its quotes, because the caller finds
    class names by scanning for string literals and an unquoted `view home`
    contains none — which is how the first version of this counted 63 classes in
    a 653-class app. A `{`-wrapped expression is returned without the braces and
    keeps every literal inside it; it ends at the brace that balances the
    opening one, counted rather than matched by regex, which is the whole reason
    nested templates work here.
    """
    if text[i] in "\"'`":
        q = text[i]
        j = text.find(q, i + 1)
        return (text[i : j + 1] if j > 0 else "", j + 1 if j > 0 else i + 1)
    if text[i] != "{":
        return "", i + 1
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j + 1
        j += 1
    return text[i + 1 :], len(text)


def used() -> dict[str, int]:
    hits: dict[str, int] = defaultdict(int)
    for f in sorted(SRC.rglob("*.ts*")):
        text = f.read_text(encoding="utf-8")
        pos = 0
        while (m := STARTS.search(text, pos)) is not None:
            expr, pos = _expr_at(text, m.end())
            expr = COMPARE.sub(" ", expr)
            raws = [next(g for g in q.groups() if g is not None) for q in QUOTED.finditer(expr)]
            raws += [t.group(1) for t in TEMPLATE.finditer(expr)]
            for raw in raws:
                for tok in INTERP.sub(" ", raw).split():
                    # A fragment like `cap-pill-` is the literal half of a class
                    # whose other half is computed at runtime; the real names
                    # cannot be enumerated from here, so it is dropped rather
                    # than reported as missing.
                    if not tok.endswith("-") and IDENT.match(tok):
                        hits[tok] += 1
    return hits


def defined() -> set[str]:
    out: set[str] = set()
    for f in sorted((SRC / "styles").glob("*.css")):
        text = f.read_text(encoding="utf-8")
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        # Selector text is everything before a `{` that is not inside a block.
        for sel in re.findall(r"([^{}]+)\{", text):
            out.update(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", sel))
    return out


def group_of(cls: str) -> str:
    return cls.split("-")[0] if "-" in cls else cls


def main() -> None:
    show_all = "--all" in sys.argv
    u, d = used(), defined()
    missing = {c: n for c, n in u.items() if c not in d}

    groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for c, n in missing.items():
        groups[group_of(c)].append((c, n))

    print(f"classes used   : {len(u)}")
    print(f"classes defined: {len(d)}")
    print(f"UNSTYLED       : {len(missing)} classes on {sum(missing.values())} elements")
    if not missing:
        print("\nnothing missing.")
    else:
        print()
        for g, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            items.sort()
            print(f"  {g + '*':<14} {len(items):>3}  " + " ".join(c for c, _ in items))

    if show_all:
        unused = sorted(c for c in d if c not in u)
        print(f"\ndefined but unused: {len(unused)}")
        for i in range(0, len(unused), 8):
            print("  " + " ".join(unused[i : i + 8]))


if __name__ == "__main__":
    main()

r"""api_audit.py — every URL the interface asks for, against every route the
server answers.

The sibling of `css_audit.py`, and it exists for the same reason: the failure is
invisible in every check that already runs. TypeScript checks that
`getRoadmapStep` returns a `RoadmapStepDetail`; it has no idea whether
`/api/roadmap/step/{id}` is a route. Rename a path in Python and the frontend
still compiles, still builds, and 404s at runtime in whichever view you did not
open. Drop a query parameter and it is worse than a 404 — `qs()` still sends it,
FastAPI ignores what it does not declare, and the response is a cheerful 200
carrying *unfiltered* rows that look exactly like an answer.

Run from web/, with the app running:  python tools/api_audit.py [--all]

    python -m server            # in another terminal
    python tools/api_audit.py

It reads the live `/api/openapi.json` rather than parsing `server/*.py`, so it
sees what FastAPI actually mounted — including routes added by a router include
that a grep for `@app.get` would miss.

## What it can and cannot prove

FastAPI's document is complete about *addresses* and silent about *bodies*: of
the 109 routes this app mounts, zero declare a response schema, because none of
the handlers annotate a `response_model` — every 200 in the document is `{}`.
That is why `npm run gen:api` produces 5,515 lines in which every response is
`unknown`, why nothing imports the result, and why `web/src/api/schema.d.ts` is
gitignored. The comment in `types.ts` about the build breaking when a view
disagrees with the server describes a check that does not exist.

So this audit deliberately checks only the half that is knowable:

  - a client URL that matches no server route          → a certain 404
  - a query parameter the server does not declare      → a silent 200, wrong data
  - a required server parameter the client never sends → a certain 422
  - a required server parameter the client's type marks optional → a 422 in
    whichever view leaves it out, since `qs()` drops an unset value rather than
    sending an empty one
  - (advisory) a route no client calls, and every call site whose parameters
    this script could not read

The last two of those four are quiet by construction: only three of the 109
routes require a query parameter at all (`/api/cell`, `/api/graph/edge`,
`/api/graph/path`) — everything else gives its parameters a Python default, so
FastAPI marks them optional and an omission is a default, not a failure. The
first two are the ones that will actually catch something.

Response *shapes* stay hand-written in `types.ts` and hand-checked. Making them
machine-checkable means annotating 109 handlers, and it fights this codebase's
own convention that a failure is `{ok: false, note}` with a 200 — not a second
model.

## How it reads the client

Two sources in `lib/api.ts`, because not every URL goes through `fetch`:

  1. the first argument of every `request<T>(...)` call;
  2. every template containing `${API}/` — the eight `*Url()` builders, whose
     output lands in an `<img src>` and never touches `request()`. Auditing only
     the fetches would have left `/api/play`, `/api/frame` and the poster tiers
     unchecked, which is half of what the Library view draws.

Both are normalised by replacing each `${...}` with `{}`, so a client
`/video/${encodeURIComponent(key)}` and a server `/api/video/{video_key}` become
the same string. Parameter *names* in a path are deliberately not compared: the
client passes them positionally and the server is free to rename them.

Query parameters come from `${qs(...)}`. Two thirds of the calls pass an object
literal, whose keys are read straight off the call site. The other ten pass a
typed bag — `searchArchive(args: SearchArgs)` — and those are the calls it
matters most to check, because `/api/search`, `/api/library`, `/api/table` and
`/api/vsearch` all take their filters that way and a filter the server has
stopped declaring comes back as a 200 carrying every row. For those,
`resolve_arg_type` reads the annotation: inline object types directly, named
interfaces from this file and then from `types.ts`. Ten of ten resolve today;
anything that stops resolving is reported under "parameters unread" rather than
guessed at, so a silent pass is never mistaken for a check.

## Trusting a clean result

A green light that cannot go red is worse than no light. The way this was
established, and the way to re-establish it after changing the parser: copy
`api.ts`, break it five ways — rename a route, add a parameter the server does
not declare, drop one it requires, add an undeclared member to `SearchArgs`, and
mark `/api/cell`'s required `table` optional — point `API_TS` at the copy, and
confirm each break lands in the bucket that claims to catch it. The parser has
already been wrong twice: `re.findall` on `(?:^|,)\s*(\w+)` consumed the comma it
matched and skipped every second key, and collapsing block comments to a single
space shifted every line number the report prints.

Known blind spots, stated so a future reader does not chase them:

  - A route reached by string concatenation rather than a template. There are
    none today; `request()` taking the path as its first argument is what keeps
    that true.
  - Whether a parameter's *value* is the right type. `?limit=all` type-checks
    as a string and 422s at runtime.
  - A parameter added to a `qs()` literal at a call site *inside a view* rather
    than in `api.ts`. Nothing does this, because `lib/api.ts` is "the one place
    that knows a URL" and there are no raw `fetch(` calls anywhere else.
  - An interface that reaches its members through `extends` or an intersection.
    `resolve_arg_type` reads one body; today every argument type is flat.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
API_TS = WEB / "src" / "lib" / "api.ts"
# Anchored to the tree, not to `API_TS.parent.parent`, so that pointing `API_TS`
# at a copy of the client — which is how the self-test proves this audit can
# still turn red — does not send the interface lookup somewhere there is no
# `types.ts`. The named-interface half of `resolve_arg_type` reads from here.
TYPES_TS = WEB / "src" / "types.ts"
OPENAPI = "http://127.0.0.1:7000/api/openapi.json"

# The leading identifier of one entry in a `qs({ ... })` object literal.
# Shorthand (`goal`) and explicit (`q: text`) both reduce to the name that
# reaches the wire. Applied per-entry rather than over the whole literal: the
# obvious `(?:^|,)\s*(\w+)` *consumes* the comma it matched on, so scanning
# `src, dst, rel, rows` found `src` and `rel` and skipped every second key —
# which reported `dst` as a required parameter the client never sends.
KEY = re.compile(r"^\s*([A-Za-z_]\w*)")

METHOD = re.compile(r"""method\s*:\s*['"](\w+)['"]""")


def _split_top(s: str) -> list[str]:
    """Split on commas that are not inside a nesting or a string.

    `qs({ keys: keys.join(',') })` is why this is not `s.split(",")`.
    """
    out: list[str] = []
    depth, start, j = 0, 0, 0
    while j < len(s):
        c = s[j]
        if c in "\"'`":
            q = c
            j += 1
            while j < len(s) and s[j] != q:
                j += 2 if s[j] == "\\" else 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(s[start:j])
            start = j + 1
        j += 1
    out.append(s[start:])
    return [p for p in (x.strip() for x in out) if p]


def _strip_nested(body: str) -> str:
    """An object-type body with every nested object removed.

    So that `query?: Record<string, unknown>` keeps its own name and a nested
    `{ a: 1 }` does not contribute `a` as a query parameter.
    """
    out, depth = [], 0
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


MEMBER = re.compile(r"\s*([A-Za-z_]\w*)\s*(\??)\s*:")


def _obj_keys(body: str) -> list[tuple[str, bool]]:
    """(name, is_optional) for each member of an object type body."""
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
    body = re.sub(r"(?m)//.*$", " ", body)
    keys: list[tuple[str, bool]] = []
    for piece in re.split(r"[;\n,]", _strip_nested(body)):
        if m := MEMBER.match(piece):
            keys.append((m.group(1), m.group(2) == "?"))
    return keys


def _balanced(src: str, i: int) -> int:
    """Index just past the `}` that closes the `{` at `i`."""
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(src)


def resolve_arg_type(text: str, at: int, ident: str, extra: str) -> list[tuple[str, bool]] | None:
    """The query parameter names behind a `qs(args)`, read from `args`' type.

    Ten of this file's calls pass a typed object rather than a literal, and they
    are the ones it matters most to check — `/api/search`, `/api/library`,
    `/api/table` and `/api/vsearch` all take their filters this way, and a filter
    the server has stopped declaring comes back as a 200 carrying every row.

    Resolution is nearest-declaration-backwards: the annotation is either inline
    (`args: { q?: string; t?: number }`) or a named interface, looked up here
    first and then in `types.ts`. Backwards and nearest because two exports both
    call their parameter `args`, and the one that counts is the one above the
    call.
    """
    win = text[max(0, at - 1200) : at]
    ms = list(re.finditer(rf"\b{re.escape(ident)}\s*:\s*", win))
    if not ms:
        return None
    rest = win[ms[-1].end() :]
    if rest.startswith("{"):
        return _obj_keys(rest[1 : _balanced(rest, 0)])
    tm = re.match(r"([A-Za-z_]\w*)", rest)
    if not tm:
        return None
    for src in (text, extra):
        if im := re.search(rf"(?:interface|type)\s+{tm.group(1)}\b[^{{;]*\{{", src):
            return _obj_keys(src[im.end() : _balanced(src, im.end() - 1)])
    return None


def _scan_args(text: str, i: int) -> tuple[list[str], int]:
    """The top-level comma-separated arguments of the call whose `(` is at `i`.

    Hand-rolled rather than regexed because every interesting argument in this
    file is a template literal holding an interpolation holding a call holding an
    object literal — `` `/roadmap${qs({ goal, breadth })}` `` — and each of the
    three nestings breaks a different flavour of pattern. Quoted strings are
    skipped whole so that a `?` or a brace inside one cannot unbalance the count.
    """
    args: list[str] = []
    depth = 0
    start = i + 1
    j = start
    while j < len(text):
        c = text[j]
        if c in "\"'":
            q = text[j]
            j += 1
            while j < len(text) and text[j] != q:
                j += 2 if text[j] == "\\" else 1
        elif c == "`":
            # A template can contain `${ ... }` containing anything, so walk it
            # with its own depth counter rather than looking for the next tick.
            j += 1
            tdepth = 0
            while j < len(text):
                if text[j] == "\\":
                    j += 1
                elif text[j] == "{" and text[j - 1] == "$":
                    tdepth += 1
                elif text[j] == "}" and tdepth:
                    tdepth -= 1
                elif text[j] == "`" and not tdepth:
                    break
                j += 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0 and c == ")":
                args.append(text[start:j])
                return [a.strip() for a in args if a.strip()], j + 1
            depth -= 1
        elif c == "," and depth == 0:
            args.append(text[start:j])
            start = j + 1
        j += 1
    return [a.strip() for a in args if a.strip()], len(text)


def _skip_type_arg(text: str, i: int) -> int:
    """Past the `<...>` of `request<T>`, given `i` just after the `<`."""
    depth, j = 1, i
    while j < len(text):
        if text[j] == "<":
            depth += 1
        elif text[j] == ">":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def _interps(tmpl: str) -> list[str]:
    """The inside of every `${...}` in a template, at the template's own level."""
    out: list[str] = []
    j = 0
    while (k := tmpl.find("${", j)) >= 0:
        depth, m = 1, k + 2
        while m < len(tmpl) and depth:
            if tmpl[m] == "{":
                depth += 1
            elif tmpl[m] == "}":
                depth -= 1
            m += 1
        out.append(tmpl[k + 2 : m - 1])
        j = m
    return out


def _path_of(expr: str) -> tuple[str, list[str], str | None]:
    """(normalised path, literal query params, the `qs(x)` identifier if any).

    The path is the template with every interpolation collapsed to `{}`, except
    that a `${qs(...)}` contributes nothing to the path — it is the query string,
    and leaving it in produced `/status{}` for a route whose real address is
    `/status`. A third return of `"args"` rather than `None` means the parameters
    are behind a type and `resolve_arg_type` has to go and read it.
    """
    body = expr.strip()
    if body[:1] in "\"'" and body[-1:] == body[:1]:
        return body[1:-1], [], None
    if not (body.startswith("`") and body.endswith("`")):
        return "", [], None
    body = body[1:-1]

    params: list[str] = []
    ident: str | None = None
    path_parts: list[str] = []
    j = 0
    for ex in _interps(body):
        k = body.index("${" + ex + "}", j)
        path_parts.append(body[j:k])
        j = k + len(ex) + 3
        stripped = ex.strip()
        if stripped.startswith("qs("):
            inner = stripped[3:-1].strip()
            if inner.startswith("{"):
                for entry in _split_top(inner[1:-1]):
                    if km := KEY.match(entry):
                        params.append(km.group(1))
            else:
                # `qs(args)` / `qs(args as Record<string, unknown>)` — the names
                # are in the parameter's type, so keep the identifier.
                ident = inner.split(" as ")[0].strip()
        elif stripped == "API":
            path_parts.append("/api")
        else:
            path_parts.append("{}")
    path_parts.append(body[j:])
    return "".join(path_parts), params, ident


def client_calls() -> list[dict]:
    """Every URL `api.ts` builds: the fetches, then the `*Url()` builders."""
    text = API_TS.read_text(encoding="utf-8")
    # Comments are blanked rather than removed, and a block comment is replaced
    # by its own newlines: this file is a third prose, so collapsing `/* … */` to
    # one space moved every line number after it. The tool's entire output is
    # "go and look at api.ts:N", which makes a shifted N worse than no N.
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"(?m)^([ \t]*)//.*$", r"\1", text)
    types = TYPES_TS.read_text(encoding="utf-8")
    calls: list[dict] = []

    for m in re.finditer(r"\brequest<", text):
        open_paren = _skip_type_arg(text, m.end())
        if open_paren >= len(text) or text[open_paren] != "(":
            continue
        args, _ = _scan_args(text, open_paren)
        if not args:
            continue
        path, params, ident = _path_of(args[0])
        if not path:
            continue
        # Optionality matters here and nowhere else: `min_dur?: number` is a
        # filter that may be absent, so it must not be reported as a required
        # parameter the client failed to send.
        optional: set[str] = set()
        readable = True
        if ident:
            resolved = resolve_arg_type(text, m.start(), ident, types)
            if resolved is None:
                readable = False
            else:
                params += [k for k, _ in resolved]
                optional |= {k for k, opt in resolved if opt}
        method = "get"
        if len(args) > 1 and (mm := METHOD.search(args[1])):
            method = mm.group(1).lower()
        line = text.count("\n", 0, m.start()) + 1
        calls.append(
            {
                "path": path,
                "method": method,
                "params": params,
                "optional": optional,
                "readable": readable,
                "line": line,
            }
        )

    # The builders. `request()` is not involved, so the only marker is `${API}`.
    # `${API}` must be followed by a literal `/`, which excludes exactly one
    # template — `request()`'s own `` `${API}${path}` `` on line 107. That one
    # is not a route, it is how every other route gets its prefix, and it
    # normalises to the address `/api{}`, which the server has never mounted.
    seen = {(c["path"], c["method"]) for c in calls}
    for m in re.finditer(r"`[^`]*\$\{API\}/[^`]*`", text):
        path, params, ident = _path_of(m.group(0))
        if path and (path, "get") not in seen:
            optional: set[str] = set()
            readable = True
            # `frameUrl(key, opts: { i?: number; t?: number })` is the one builder
            # that takes a bag rather than positional arguments, so it needs the
            # same type lookup the fetches get. Without it the two parameters that
            # decide *which frame* went unchecked.
            if ident:
                resolved = resolve_arg_type(text, m.start(), ident, types)
                if resolved is None:
                    readable = False
                else:
                    params += [k for k, _ in resolved]
                    optional |= {k for k, opt in resolved if opt}
            line = text.count("\n", 0, m.start()) + 1
            calls.append(
                {
                    "path": path,
                    "method": "get",
                    "params": params,
                    "optional": optional,
                    "readable": readable,
                    "line": line,
                    "builder": True,
                }
            )
            seen.add((path, "get"))
    return calls


def server_routes() -> dict[tuple[str, str], dict]:
    try:
        doc = json.load(urllib.request.urlopen(OPENAPI, timeout=8))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        sys.exit(f"cannot read {OPENAPI}: {e}\nstart the app first:  python -m server")
    out: dict[tuple[str, str], dict] = {}
    for raw, ops in (doc.get("paths") or {}).items():
        norm = re.sub(r"\{[^}]*\}", "{}", raw)
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            q = {
                p["name"]: bool(p.get("required"))
                for p in (op.get("parameters") or [])
                if p.get("in") == "query"
            }
            out[(norm, method)] = {"raw": raw, "query": q}
    return out


def main() -> None:
    show_all = "--all" in sys.argv
    calls = client_calls()
    routes = server_routes()

    # `/api` is added by `request()` for a path that does not already carry it.
    for c in calls:
        if not c["path"].startswith("/api"):
            c["path"] = "/api" + c["path"]

    no_route: list[dict] = []
    bad_param: list[tuple[dict, list[str]]] = []
    missing_req: list[tuple[dict, list[str]]] = []
    soft_req: list[tuple[dict, list[str]]] = []
    unread: list[dict] = []

    for c in calls:
        key = (c["path"], c["method"])
        route = routes.get(key)
        if route is None:
            no_route.append(c)
            continue
        if not c["readable"]:
            unread.append(c)
            continue
        undeclared = [p for p in c["params"] if p not in route["query"]]
        if undeclared:
            bad_param.append((c, undeclared))
        absent = [p for p, req in route["query"].items() if req and p not in c["params"]]
        if absent:
            missing_req.append((c, absent))
        # The client *can* send it, but its type says it may be left out — and
        # `qs()` drops an unset value rather than sending an empty one, so the
        # request that omits it is a 422 waiting for whichever view forgets.
        soft = [
            p
            for p, req in route["query"].items()
            if req and p in c["optional"]
        ]
        if soft:
            soft_req.append((c, soft))

    called = {(c["path"], c["method"]) for c in calls}
    uncalled = sorted(k for k in routes if k not in called)

    print(f"client URLs  : {len(calls)}  ({sum(1 for c in calls if c.get('builder'))} are src= builders)")
    print(f"server routes: {len(routes)}")
    print(
        f"BROKEN       : {len(no_route)} unrouted, {len(bad_param)} undeclared params, "
        f"{len(missing_req) + len(soft_req)} required-but-optional"
    )

    if no_route:
        print("\n-- no such route (404) --")
        for c in sorted(no_route, key=lambda c: c["path"]):
            print(f"  api.ts:{c['line']:<4} {c['method'].upper():<5} {c['path']}")
    if bad_param:
        print("\n-- server does not declare these (silently ignored, 200 with wrong data) --")
        for c, ps in sorted(bad_param, key=lambda t: t[0]["path"]):
            print(f"  api.ts:{c['line']:<4} {c['method'].upper():<5} {c['path']}  ?{' ?'.join(ps)}")
    if missing_req:
        print("\n-- required by the server, never sent (422) --")
        for c, ps in sorted(missing_req, key=lambda t: t[0]["path"]):
            print(f"  api.ts:{c['line']:<4} {c['method'].upper():<5} {c['path']}  needs {', '.join(ps)}")
    if soft_req:
        print("\n-- required by the server, optional in the client's type (422 when omitted) --")
        for c, ps in sorted(soft_req, key=lambda t: t[0]["path"]):
            print(f"  api.ts:{c['line']:<4} {c['method'].upper():<5} {c['path']}  {', '.join(p + '?' for p in ps)}")
    if not (no_route or bad_param or missing_req or soft_req):
        print("\nevery URL the interface builds is a route, with parameters the server declares.")

    if show_all:
        print(f"\nparameters unread ({len(unread)}) -- `qs(args)`, names live in the type:")
        for c in sorted(unread, key=lambda c: c["path"]):
            print(f"  api.ts:{c['line']:<4} {c['method'].upper():<5} {c['path']}")
        by_prefix: dict[str, list[str]] = defaultdict(list)
        for path, method in uncalled:
            by_prefix["/".join(path.split("/")[:3])].append(f"{method.upper()} {path}")
        print(f"\nmounted but never called ({len(uncalled)}):")
        for pre, items in sorted(by_prefix.items()):
            print(f"  {pre}")
            for it in sorted(items):
                print(f"    {it}")


if __name__ == "__main__":
    main()

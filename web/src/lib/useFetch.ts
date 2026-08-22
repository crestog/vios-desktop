/**
 * lib/useFetch.ts — one loading story, used by every view.
 *
 * Eleven views each doing their own `useEffect` + `useState` + abort dance is
 * eleven chances to get the same three things wrong, and one of them is not
 * obvious: **the previous data has to stay on screen while the next request is
 * in flight.** Blanking to a spinner on every keystroke makes a 90 ms search
 * feel slower than a 400 ms one, because the eye reads the flash as work. So
 * `data` is only replaced on success, and `loading` is a flag beside it rather
 * than a state that replaces it.
 *
 * The other two: an aborted request is not an error (it is the newer keystroke
 * doing its job, so it must not paint a red box), and a response that arrives
 * after the component unmounted must not be written to state.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, isAborted } from './api';

export interface FetchState<T> {
  data: T | null;
  /** A message ready to show. `null` when there is nothing wrong. */
  error: string | null;
  loading: boolean;
  /** True until the first successful response — the only time a skeleton is right. */
  first: boolean;
  reload: () => void;
}

export function useFetch<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
  opts: { enabled?: boolean } = {}
): FetchState<T> {
  const enabled = opts.enabled !== false;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [first, setFirst] = useState(true);
  const [nonce, setNonce] = useState(0);

  // The callback identity changes on every render for most callers (they pass
  // an arrow), so it is held in a ref and the effect keys off `deps` instead.
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    fnRef
      .current(ctrl.signal)
      .then((got) => {
        if (!alive.current || ctrl.signal.aborted) return;
        setData(got);
        setError(null);
        setFirst(false);
        setLoading(false);
      })
      .catch((e) => {
        if (!alive.current || isAborted(e)) return;
        setError(
          e instanceof ApiError
            ? e.status === 0
              ? 'The local server is not answering.'
              : `${e.detail} (${e.status})`
            : String((e as Error)?.message || e)
        );
        setFirst(false);
        setLoading(false);
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, first, reload };
}

/**
 * Hold a value back until it stops changing.
 *
 * 120 ms is the budget for keystroke → painted results, and this spends part of
 * it deliberately: firing on every character would issue eight requests for
 * "hook line" and abort seven, which costs the server eight FTS5 queries to
 * paint one. 140 ms is short enough to feel immediate while typing at speed and
 * long enough that a whole word usually arrives as one request.
 */
export function useDebounced<T>(value: T, ms = 140): T {
  const [held, setHeld] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setHeld(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return held;
}

/** An element's live size, for anything that has to do its own layout maths. */
export function useSize<E extends HTMLElement>(ref: React.RefObject<E>): { w: number; h: number } {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (!box) return;
      setSize((prev) =>
        // Integer compare: a ResizeObserver fires on sub-pixel changes during a
        // CSS transition, and a grid that recomputes its column width 60 times
        // for a 0.3 px difference drops frames for nothing.
        Math.round(box.width) === prev.w && Math.round(box.height) === prev.h
          ? prev
          : { w: Math.round(box.width), h: Math.round(box.height) }
      );
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}

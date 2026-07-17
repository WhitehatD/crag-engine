import { useCallback } from "react";
import { useNavigate, useRouterState } from "@tanstack/react-router";

// useUrlState — read/write a single query-param as view state so filters,
// pagination, and selected tabs are shareable/bookmarkable deep links. Values
// are strings in the URL; callers coerce (e.g. Number for offsets).
//
// Writes REPLACE history (filter tweaks shouldn't spam the back button) and
// preserve every other param (embed, glossary, sibling filters) untouched.
export function useUrlState(
  key: string,
  fallback = "",
): [string, (v: string) => void] {
  const navigate = useNavigate();
  const search = useRouterState({ select: (s) => s.location.search }) as Record<
    string,
    unknown
  >;
  const raw = search?.[key];
  const value = raw === undefined || raw === null ? fallback : String(raw);

  const set = useCallback(
    (v: string) => {
      navigate({
        to: ".",
        search: (prev: Record<string, string | number | undefined>) => {
          const next: Record<string, string | number | undefined> = { ...prev };
          if (v === "" || v === fallback) delete next[key];
          else next[key] = v;
          return next;
        },
        replace: true,
      });
    },
    [key, fallback, navigate],
  );

  return [value, set];
}

// Numeric convenience over useUrlState (for offsets/pages).
export function useUrlNumber(
  key: string,
  fallback = 0,
): [number, (v: number) => void] {
  const [s, set] = useUrlState(key, String(fallback));
  const n = Number(s);
  return [Number.isFinite(n) ? n : fallback, (v: number) => set(String(v))];
}

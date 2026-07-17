import { useState, useId, type ReactNode } from "react";
import { IconChevron } from "./icons";
import { lookupTerm, type Term } from "../lib/glossary";

// -----------------------------------------------------------------------------
// Tooltip — a hover/tap definition bubble. No library: a positioned <span> that
// appears on hover (desktop) or on tap-toggle (touch). Kept inside the trigger
// so it inherits flow position; the bubble itself is absolutely placed.
// -----------------------------------------------------------------------------
export function Tooltip({
  label,
  children,
}: {
  label: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span
      className="relative inline-flex"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onClick={() => setOpen((v) => !v)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <span aria-describedby={open ? id : undefined} tabIndex={0} className="outline-none">
        {children}
      </span>
      {open && (
        <span
          id={id}
          role="tooltip"
          className="absolute left-1/2 top-full z-50 mt-1.5 w-max max-w-[240px] -translate-x-1/2 rounded-[7px] border border-[var(--color-border)] bg-[var(--color-surface-2)] px-2.5 py-1.5 text-left text-[11px] font-normal leading-snug text-[var(--color-text)]"
          style={{ fontFamily: "var(--font-sans)", pointerEvents: "none" }}
        >
          {label}
        </span>
      )}
    </span>
  );
}

// Wrap any chip token with its glossary definition. If the token isn't in the
// glossary the child renders bare (no empty tooltip).
export function Defined({ token, children }: { token: string; children: ReactNode }) {
  const t = lookupTerm(token);
  if (!t) return <>{children}</>;
  return (
    <Tooltip
      label={
        <>
          <span className="font-medium text-[var(--color-text)]">{t.term}</span>
          <span className="mt-0.5 block text-[var(--color-muted)]">{t.short}</span>
        </>
      }
    >
      {children}
    </Tooltip>
  );
}

// -----------------------------------------------------------------------------
// AboutPanel — the expandable "About this view" block under every view title.
// Dismiss/expand state persists per-view in localStorage.
// -----------------------------------------------------------------------------
export function AboutPanel({
  id,
  title,
  subtitle,
  children,
  defaultOpen = false,
}: {
  id: string;
  title: string;
  subtitle: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const key = `crag-console:about:${id}`;
  const [open, setOpen] = useState(() => {
    try {
      const v = localStorage.getItem(key);
      return v === null ? defaultOpen : v === "1";
    } catch {
      return defaultOpen;
    }
  });
  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      try {
        localStorage.setItem(key, next ? "1" : "0");
      } catch {
        /* private mode */
      }
      return next;
    });
  };
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-[17px] font-semibold tracking-tight text-[var(--color-text)]">
          {title}
        </h1>
        <p className="text-[12px] text-[var(--color-muted)]">{subtitle}</p>
        <button
          onClick={toggle}
          aria-expanded={open}
          className="ml-auto flex items-center gap-1 rounded-[7px] border border-[var(--color-border)] px-2 py-0.5 text-[11px] text-[var(--color-muted)] transition-colors hover:text-[var(--color-text)]"
        >
          About this view
          <IconChevron
            size={12}
            className={"transition-transform " + (open ? "rotate-180" : "")}
          />
        </button>
      </div>
      {open && (
        <div className="mt-2 rounded-[12px] border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--color-muted)]">
          {children}
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------------
// ViewHeader — the standard top block for every view: title + one-line purpose
// subtitle + expandable About panel, with an optional refresh control on the
// right. One component so every view is self-explanatory the same way.
// -----------------------------------------------------------------------------
export function ViewHeader({
  id,
  title,
  subtitle,
  about,
  right,
}: {
  id: string;
  title: string;
  subtitle: string;
  about: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="space-y-2">
      <AboutPanel id={id} title={title} subtitle={subtitle}>
        {about}
      </AboutPanel>
      {right && <div className="flex items-center justify-end">{right}</div>}
    </div>
  );
}

// -----------------------------------------------------------------------------
// TeachingEmpty — an empty state that explains what WOULD populate the table and
// which stage of the loop feeds it. Never a bare "No data".
// -----------------------------------------------------------------------------
export function TeachingEmpty({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-1.5 px-4 py-10 text-center">
      <div className="text-[13px] font-medium text-[var(--color-text)]">{title}</div>
      <div className="max-w-md text-[12px] leading-relaxed text-[var(--color-muted)]">
        {children}
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// Skeleton — flat pulse placeholder (no shimmer sweep; opacity pulse only).
// -----------------------------------------------------------------------------
export function Skeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-7 animate-pulse rounded-[7px] bg-[var(--color-surface-2)]"
          style={{ animationDelay: `${i * 60}ms`, opacity: 0.6 }}
        />
      ))}
    </div>
  );
}

// -----------------------------------------------------------------------------
// GlossaryTermList — reused by the drawer. Renders a section of terms.
// -----------------------------------------------------------------------------
export function TermList({ heading, terms }: { heading: string; terms: Record<string, Term> }) {
  return (
    <div>
      <div className="mb-1.5 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
        {heading}
      </div>
      <dl className="space-y-1.5">
        {Object.entries(terms).map(([k, t]) => (
          <div
            key={k}
            className="rounded-[7px] border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2"
          >
            <dt className="text-[12px] font-medium text-[var(--color-text)]">{t.term}</dt>
            <dd className="mt-0.5 text-[12px] leading-snug text-[var(--color-muted)]">
              {t.long ?? t.short}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Link } from "@tanstack/react-router";
import type { Verdict } from "../lib/types";
import { Defined, Tooltip } from "./explain";
import { IconRefresh, IconClose } from "./icons";

// Hand-rolled primitives in the crag theme. No component library.

export function Section({
  title,
  right,
  children,
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[12px] border border-[var(--color-border)] bg-[var(--color-surface)]">
      {(title || right) && (
        <header className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-2.5">
          <h2 className="text-[13px] font-medium tracking-tight text-[var(--color-text)]">
            {title}
          </h2>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function StatCard({
  label,
  value,
  sub,
  to,
  search,
  tone = "default",
  hint,
  spark,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  // `to` is a router path (e.g. "/claims"); the card client-navigates, keeping
  // ?embed and other search params intact. `search` presets filters on arrival.
  to?: string;
  search?: Record<string, string>;
  tone?: "default" | "brand" | "warn" | "danger";
  hint?: string;
  spark?: ReactNode;
}) {
  const toneColor = {
    default: "var(--color-text)",
    brand: "var(--color-brand)",
    warn: "var(--color-amber)",
    danger: "var(--color-red)",
  }[tone];
  const inner = (
    <div
      className={
        "flex h-full flex-col rounded-[12px] border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 transition-colors " +
        (to ? "hover:border-[var(--color-focus)]" : "")
      }
    >
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
          {label}
        </span>
        {to && (
          <span className="ml-auto text-[var(--color-muted)] opacity-60" aria-hidden>
            ↗
          </span>
        )}
      </div>
      <div className="num mt-1 text-2xl leading-none" style={{ color: toneColor }}>
        {value}
      </div>
      {sub && <div className="mt-1 text-[11px] text-[var(--color-muted)]">{sub}</div>}
      {spark && <div className="mt-2">{spark}</div>}
    </div>
  );
  const wrapped = hint ? (
    <Tooltip label={hint}>{inner}</Tooltip>
  ) : (
    inner
  );
  return to ? (
    <Link
      to={to}
      search={search as never}
      className="block h-full outline-none"
      title={hint}
    >
      {inner}
    </Link>
  ) : (
    wrapped
  );
}

const VERDICT_TONE: Record<string, { fg: string; bg: string }> = {
  fresh: { fg: "#22c55e", bg: "rgba(34,197,94,0.12)" },
  aging: { fg: "#f59e0b", bg: "rgba(245,158,11,0.12)" },
  unverified: { fg: "#a1a1aa", bg: "rgba(161,161,170,0.12)" },
  revalidating: { fg: "#f97316", bg: "rgba(249,115,22,0.12)" },
  stale: { fg: "#ef4444", bg: "rgba(239,68,68,0.12)" },
  axiomatic: { fg: "#22d3ee", bg: "rgba(34,211,238,0.12)" },
};

const CLASS_TONE: Record<string, string> = {
  P1: "#22c55e",
  P2: "#22d3ee",
  P3: "#f59e0b",
  P4: "#a78bfa",
  P5: "#a1a1aa",
};

export function Chip({
  children,
  fg,
  bg,
  title,
}: {
  children: ReactNode;
  fg?: string;
  bg?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className="num inline-flex items-center rounded-[6px] px-1.5 py-0.5 text-[11px] font-medium"
      style={{
        color: fg ?? "var(--color-muted)",
        background: bg ?? "var(--color-surface-2)",
        border: `1px solid ${bg ? "transparent" : "var(--color-border)"}`,
      }}
    >
      {children}
    </span>
  );
}

export function VerdictChip({ verdict }: { verdict: Verdict | string }) {
  const t = VERDICT_TONE[verdict] ?? VERDICT_TONE.unverified;
  return (
    <Defined token={String(verdict)}>
      <Chip fg={t.fg} bg={t.bg}>
        {verdict}
      </Chip>
    </Defined>
  );
}

export function ClassChip({ pclass }: { pclass: string | null }) {
  if (!pclass) return <Chip>—</Chip>;
  const fg = CLASS_TONE[pclass] ?? "var(--color-muted)";
  return (
    <Defined token={pclass}>
      <Chip fg={fg} bg="var(--color-surface-2)">
        {pclass}
      </Chip>
    </Defined>
  );
}

// A tier badge (t0/t1/t2) with its glossary definition on hover/tap.
export function TierChip({ tier }: { tier: string | null | undefined }) {
  const t = (tier ?? "").toLowerCase();
  return (
    <Defined token={t}>
      <Chip fg="#22d3ee">{(tier ?? "?").toUpperCase()}</Chip>
    </Defined>
  );
}

export function Drawer({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    // On <640px the drawer is a full-screen sheet (justify-end still, but the
    // aside takes full width and the backdrop is edge-to-edge). On >=640px it is
    // the familiar right-hand rail.
    <div className="fixed inset-0 z-40 flex justify-end">
      <div
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-hidden
      />
      <aside className="relative z-50 flex h-full w-full flex-col border-l border-[var(--color-border)] bg-[var(--color-surface)] sm:max-w-2xl">
        <header className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
          <div className="text-[13px] font-medium">{title}</div>
          <button
            onClick={onClose}
            aria-label="close"
            className="flex h-9 w-9 items-center justify-center rounded-[7px] border border-[var(--color-border)] text-[var(--color-muted)] hover:text-[var(--color-text)]"
          >
            <IconClose size={15} />
          </button>
        </header>
        <div
          className="flex-1 overflow-auto p-4"
          style={{ paddingBottom: "max(1rem, env(safe-area-inset-bottom))" }}
        >
          {children}
        </div>
      </aside>
    </div>
  );
}

export function Sparkline({
  values,
  color = "var(--color-brand)",
  width = 120,
  height = 28,
}: {
  values: number[];
  color?: string;
  width?: number;
  height?: number;
}) {
  if (!values.length) return <span className="text-[var(--color-muted)]">—</span>;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const pts = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / span) * height).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

// Responsive table. Desktop (>=768px): a real <table>. Mobile (<768px): every
// row collapses to a stacked card and every cell prefixes its column label
// (carried via context so views never fork markup per breakpoint). Driven by
// the `.rtable` CSS in theme.css.
const TableHeadCtx = createContext<ReactNode[]>([]);

export function Table({
  head,
  children,
}: {
  head: ReactNode[];
  children: ReactNode;
}) {
  return (
    <TableHeadCtx.Provider value={head}>
      {/* Desktop: contained horizontal scroll if columns overflow (never page-
          level). Mobile: rtable stacks rows into cards, so nothing overflows. */}
      <div className="md:overflow-x-auto">
        <table className="rtable w-full border-collapse text-[13px]">
          <thead>
            <tr className="border-b border-[var(--color-border)] text-left text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
              {head.map((h, i) => (
                <th key={i} className="px-3 py-2 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>{children}</tbody>
        </table>
      </div>
    </TableHeadCtx.Provider>
  );
}

// Row context so each Cell can look up its own column label for the mobile card.
const CellIndexCtx = createContext<{ next: () => number } | null>(null);

export function Row({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick?: () => void;
}) {
  // A fresh counter per row so cells resolve their column label positionally.
  let i = 0;
  const provider = { next: () => i++ };
  return (
    <CellIndexCtx.Provider value={provider}>
      <tr
        onClick={onClick}
        className={
          "border-b border-[var(--color-border)]/60 " +
          (onClick
            ? "cursor-pointer hover:bg-[var(--color-surface-2)] active:bg-[var(--color-surface-2)]"
            : "")
        }
      >
        {children}
      </tr>
    </CellIndexCtx.Provider>
  );
}

export function Cell({ children, mono }: { children: ReactNode; mono?: boolean }) {
  const head = useContext(TableHeadCtx);
  const idx = useContext(CellIndexCtx);
  const col = idx ? idx.next() : -1;
  const label = col >= 0 ? head[col] : undefined;
  return (
    <td
      className={"px-3 py-2 align-top " + (mono ? "num" : "")}
      data-label={typeof label === "string" ? label : undefined}
    >
      {children}
    </td>
  );
}

export function Empty({ label }: { label: ReactNode }) {
  return (
    <div className="px-3 py-10 text-center text-[13px] text-[var(--color-muted)]">
      {label}
    </div>
  );
}

// RefreshBar — "updated Xs ago" + a manual refresh button that pulses while a
// refetch is in flight. Pass react-query's dataUpdatedAt + isFetching + refetch.
export function RefreshBar({
  updatedAt,
  isFetching,
  onRefresh,
}: {
  updatedAt: number | undefined;
  isFetching: boolean;
  onRefresh: () => void;
}) {
  const [, force] = useState(0);
  // Tick every 5s so "Xs ago" stays live without a data change.
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 5000);
    return () => clearInterval(t);
  }, []);
  const ago = updatedAt ? Math.max(0, Math.round((Date.now() - updatedAt) / 1000)) : null;
  return (
    <div className="flex items-center gap-2 text-[11px] text-[var(--color-muted)]">
      <span
        className="inline-block h-1.5 w-1.5 rounded-full transition-colors"
        style={{ background: isFetching ? "var(--color-focus)" : "var(--color-border)" }}
        aria-hidden
      />
      <span className="num">
        {ago === null ? "—" : ago === 0 ? "just now" : `updated ${ago}s ago`}
      </span>
      <button
        onClick={onRefresh}
        aria-label="refresh now"
        className="flex h-7 w-7 items-center justify-center rounded-[7px] border border-[var(--color-border)] text-[var(--color-muted)] hover:text-[var(--color-text)]"
      >
        <IconRefresh size={13} className={isFetching ? "animate-spin" : ""} />
      </button>
    </div>
  );
}

export function Btn({
  children,
  onClick,
  tone = "default",
  disabled,
}: {
  children: ReactNode;
  onClick?: () => void;
  tone?: "default" | "brand" | "danger";
  disabled?: boolean;
}) {
  const border = {
    default: "var(--color-border)",
    brand: "var(--color-brand)",
    danger: "var(--color-red)",
  }[tone];
  const fg = {
    default: "var(--color-text)",
    brand: "var(--color-brand)",
    danger: "var(--color-red)",
  }[tone];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="rounded-[7px] border px-2.5 py-1 text-[12px] transition-colors hover:bg-[var(--color-surface-2)] disabled:opacity-40"
      style={{ borderColor: border, color: fg }}
    >
      {children}
    </button>
  );
}

export function Input({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="num rounded-[7px] border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1 text-[12px] text-[var(--color-text)] placeholder:text-[var(--color-muted)]"
    />
  );
}

export function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-[7px] border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 text-[12px] text-[var(--color-text)]"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function Pager({
  offset,
  limit,
  total,
  onPage,
}: {
  offset: number;
  limit: number;
  total: number;
  onPage: (offset: number) => void;
}) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <div className="flex items-center gap-2 text-[12px] text-[var(--color-muted)]">
      <span className="num">
        {from}–{to} of {total.toLocaleString()}
      </span>
      <Btn onClick={() => onPage(Math.max(0, offset - limit))} disabled={offset === 0}>
        prev
      </Btn>
      <Btn onClick={() => onPage(offset + limit)} disabled={to >= total}>
        next
      </Btn>
    </div>
  );
}

export function truncate(s: string, n = 90): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export function ageOf(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso.replace(" ", "T"));
  if (Number.isNaN(t)) return "—";
  const sec = (Date.now() - t) / 1000;
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h`;
  return `${Math.round(sec / 86400)}d`;
}

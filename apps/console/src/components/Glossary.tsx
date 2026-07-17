import { Drawer } from "./primitives";
import { TermList } from "./explain";
import {
  LOOP_STAGES,
  PREDICATE_CLASSES,
  VERDICTS,
  TIERS,
  STATUSES,
  TRUST_STATEMENT,
} from "../lib/glossary";

// The loop diagram — pure CSS/SVG, no library. A vertical chain of the seven
// stages with connector arrows; each stage names what it does. Readable at any
// width (it just stacks).
function LoopDiagram() {
  return (
    <ol className="space-y-0">
      {LOOP_STAGES.map((s, i) => (
        <li key={s.key} className="relative">
          <div className="flex items-start gap-3">
            <div className="flex flex-col items-center">
              <span
                className="num flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[11px]"
                style={{
                  borderColor: "var(--color-focus)",
                  color: "var(--color-focus)",
                }}
              >
                {i + 1}
              </span>
              {i < LOOP_STAGES.length - 1 && (
                <span
                  className="my-0.5 w-px flex-1"
                  style={{ minHeight: 18, background: "var(--color-border)" }}
                  aria-hidden
                />
              )}
            </div>
            <div className="pb-3">
              <div className="text-[13px] font-medium text-[var(--color-text)]">
                {s.title}
              </div>
              <div className="mt-0.5 text-[12px] leading-snug text-[var(--color-muted)]">
                {s.blurb}
              </div>
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}

export default function Glossary({ onClose }: { onClose: () => void }) {
  return (
    <Drawer open onClose={onClose} title="Glossary — how the loop works">
      <div className="space-y-6">
        <section>
          <div className="mb-2 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
            The closed loop
          </div>
          <p className="mb-3 text-[12.5px] leading-relaxed text-[var(--color-muted)]">
            crag turns agent failures into enforced governance. Each console view
            fronts one live stage of this loop:
          </p>
          <LoopDiagram />
        </section>

        <TermList heading="Predicate classes (how a claim is checked)" terms={PREDICATE_CLASSES} />
        <TermList heading="Verdicts (a claim's liveness)" terms={VERDICTS} />
        <TermList heading="Disposition tiers (who may accept)" terms={TIERS} />
        <TermList heading="Statuses" terms={STATUSES} />

        <p
          className="rounded-[12px] border px-4 py-3 text-[12.5px] italic leading-relaxed"
          style={{
            borderColor: "var(--color-border)",
            color: "var(--color-text)",
            background: "var(--color-surface)",
          }}
        >
          {TRUST_STATEMENT}
        </p>
      </div>
    </Drawer>
  );
}

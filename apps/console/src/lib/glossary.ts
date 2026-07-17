// GLOSSARY — the single source of truth for every plain-language definition
// surfaced anywhere in the console: chip tooltips, the "About this view" panels,
// the Glossary drawer's term list, and empty-state copy. One constant so a term
// never drifts between two places it appears.

export interface Term {
  term: string;
  short: string; // one sentence — used for chip tooltips
  long?: string; // optional extra context — used in the Glossary drawer
}

// Predicate classes — how a claim is machine-checked.
export const PREDICATE_CLASSES: Record<string, Term> = {
  P1: {
    term: "P1 — mechanical",
    short: "Verified by running a shell probe against live reality.",
    long: "A P1 claim carries an executable command. Grounding runs it and reads the exit code / output to decide fresh vs stale — the cheapest, most objective check.",
  },
  P2: {
    term: "P2 — documentary",
    short: "Verified against a source anchor (a file, line, or doc it cites).",
    long: "A P2 claim points at a specific artifact. Grounding confirms the anchor still exists and still says what the claim says.",
  },
  P3: {
    term: "P3 — temporal",
    short: "Verified by a time or freshness condition.",
    long: "A P3 claim is true only within a window (e.g. 'released in the last 30 days'). Grounding re-checks the clock.",
  },
  P4: {
    term: "P4 — semantic",
    short: "Verified by an LLM judging meaning — no mechanical probe exists.",
    long: "A P4 claim is a judgement call. An LLM reads the claim and current evidence and returns pass / fail / uncertain. The most expensive lane.",
  },
  P5: {
    term: "P5 — axiomatic",
    short: "A policy axiom — held true by decree, never probed.",
    long: "A P5 claim is a governance axiom. It has no falsifier and is terminal: its verdict is 'axiomatic' and grounding never touches it.",
  },
};

// Verdicts — the liveness state of a claim after grounding.
export const VERDICTS: Record<string, Term> = {
  fresh: {
    term: "fresh",
    short: "Re-verified against reality recently — trustworthy right now.",
    long: "The claim passed its most recent grounding run within its volatility window.",
  },
  aging: {
    term: "aging",
    short: "Last verified a while ago — still passing but drifting toward re-check.",
  },
  unverified: {
    term: "unverified",
    short: "Never grounded yet — no evidence for or against.",
  },
  revalidating: {
    term: "revalidating",
    short: "Queued for a fresh grounding run — discount until it resolves.",
  },
  stale: {
    term: "stale",
    short: "Failed its last check or aged out — do not trust without re-verifying.",
  },
  axiomatic: {
    term: "axiomatic",
    short: "A P5 policy axiom — terminal, held true by decree.",
  },
};

// Disposition tiers — who is allowed to accept a captured proposal.
export const TIERS: Record<string, Term> = {
  t0: {
    term: "T0 — auto",
    short: "Low-risk: the engine accepts it automatically, no human needed.",
  },
  t1: {
    term: "T1 — agent",
    short: "An agent with a granted capability may accept it.",
  },
  t2: {
    term: "T2 — human",
    short: "High-stakes: requires explicit human approval to accept.",
  },
};

// Disposition / proposal statuses.
export const STATUSES: Record<string, Term> = {
  pending: { term: "pending", short: "Awaiting triage — not yet accepted or rejected." },
  accepted: { term: "accepted", short: "Promoted from staging into the corpus as an insight." },
  rejected: { term: "rejected", short: "Dropped — kept only as a memory-only record." },
  deferred: { term: "deferred", short: "Left pending on purpose, to revisit later." },
  done: { term: "done", short: "A grounding job that finished." },
  running: { term: "running", short: "A grounding job currently executing." },
  failed: { term: "failed", short: "A grounding job that errored and may retry." },
};

// The loop stages, in order — used by the Glossary drawer diagram and each
// view's "About this view" panel.
export interface LoopStage {
  key: string;
  title: string;
  blurb: string;
  view: string; // the console route that fronts this stage
}

export const LOOP_STAGES: LoopStage[] = [
  {
    key: "capture",
    title: "Capture",
    blurb:
      "Agent failures and lessons are captured into a staging area the moment they happen.",
    view: "/review",
  },
  {
    key: "disposition",
    title: "Disposition",
    blurb:
      "The triage engine sorts each captured item into a tier — T0 auto, T1 agent, T2 human — and routes it accordingly.",
    view: "/review",
  },
  {
    key: "insight",
    title: "Insight",
    blurb:
      "Accepted items become insights: raw, confidence-scored memory the engine can recall.",
    view: "/corpus",
  },
  {
    key: "claim",
    title: "Claim",
    blurb:
      "Each insight is decomposed into atomic claims, each with an executable predicate (P1–P5).",
    view: "/claims",
  },
  {
    key: "grounding",
    title: "Grounding",
    blurb:
      "Claims are machine-verified against live reality. Verdicts: fresh, aging, unverified, revalidating, stale.",
    view: "/grounding",
  },
  {
    key: "principle",
    title: "Principle",
    blurb:
      "Verified recurring patterns are promoted to principles — the highest-trust layer, with their own confidence lifecycle.",
    view: "/corpus",
  },
  {
    key: "distill",
    title: "Distill",
    blurb:
      "Compile-eligible principles (all core claims fresh) are distilled by crag into enforced governance rules.",
    view: "/",
  },
];

// Flat lookup used by chips: pass a raw token, get its Term (or undefined).
export function lookupTerm(token: string): Term | undefined {
  const t = token.toLowerCase();
  return (
    PREDICATE_CLASSES[token] ??
    PREDICATE_CLASSES[token.toUpperCase()] ??
    VERDICTS[t] ??
    TIERS[t] ??
    STATUSES[t]
  );
}

// The trust statement, stated once — shown in the Glossary drawer footer.
export const TRUST_STATEMENT =
  "Trust is how recently a claim was re-verified against reality — not a number that only rises.";

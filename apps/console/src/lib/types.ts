// Response shapes — mirror the daemon endpoints verified against live data.

export type Verdict =
  | "fresh"
  | "aging"
  | "unverified"
  | "revalidating"
  | "stale"
  | "axiomatic";

export interface Liveness {
  verdict: Verdict;
  grounded_at: string | null;
  age_days: number | null;
  volatility_class: string | null;
  grounding_due: boolean;
  falsifier: string;
}

export interface ClaimRow {
  id: number;
  text: string;
  predicate_class: string | null;
  verdict: Verdict | string;
  primary_entity: string | null;
  primary_entity_type: string | null;
  grounded_at: string | null;
  insight_parents: number;
  principle_parents: number;
}

export interface ClaimsResponse {
  ok: boolean;
  total: number;
  limit: number;
  offset: number;
  claims: ClaimRow[];
  error?: string;
}

export interface ClaimDetail {
  ok: boolean;
  claim: Record<string, unknown> & { id: number; text: string; verdict?: string };
  predicate_spec: unknown;
  entities: { entity: string; entity_type: string; canonical_entity_id: number | null }[];
  parents: {
    insights: { id: number; role: string; preview: string; project: string | null }[];
    principles: { id: number; role: string; preview: string; project: string | null }[];
  };
  grounding_history: {
    ts: string;
    job_type: string;
    verdict: string | null;
    reasoning: string | null;
    evidence: string | null;
    lane: string | null;
    recipe_version: number | null;
  }[];
  error?: string;
}

export interface ClaimSide {
  id: number;
  text: string;
  predicate_class: string | null;
  verdict: string;
  primary_entity: string | null;
}

export interface ContradictionPair {
  id: number;
  status: string;
  reason: string | null;
  score: number | null;
  detected_at: string;
  resolved_at: string | null;
  shared_entity: string | null;
  claim_a: ClaimSide | null;
  claim_b: ClaimSide | null;
}

export interface ContradictionsResponse {
  ok: boolean;
  total: number;
  pairs: ContradictionPair[];
  error?: string;
}

export interface Stats {
  requests_served: number;
  db_size_bytes: number;
  insight_counts: { active: number; total: number; principles: number };
  uptime_seconds: number;
}

export interface GroundStats {
  ok: boolean;
  queue_by_status: Record<string, number>;
  queue_failed_split?: Record<string, number>;
  oldest_pending_age_sec: number;
  jobs_done_last_24h: number;
  verdict_dist_last_24h: Record<string, number>;
  flagged_claims: { insights: number; principles: number };
  coverage_by_class: Record<string, number>;
  pass_rate_by_class: Record<string, number>;
}

export interface HealthCheck {
  ok: boolean;
  checks: { class: string; severity: string; status: string; detail: string }[];
  summary: string;
}

export interface DispositionEntry {
  id: number;
  source: string;
  project: string | null;
  payload: string;
  status: string;
  created_at: string;
  reason: string | null;
  lifecycle_action: string | null;
  tier: string;
  actor: string | null;
  deadline: string | null;
  disposition: string | null;
}

export interface DispositionListResponse {
  ok: boolean;
  count: number;
  by_tier: Record<string, number>;
  entries: DispositionEntry[];
}

export interface GroundJob {
  id: number;
  claim_kind: string;
  claim_id: number;
  job_type: string;
  status: string;
  attempts: number;
  enqueued_at: string;
  started_at: string | null;
  finished_at: string | null;
  last_error: string;
}

export interface InsightRow {
  id: number;
  content: string;
  type: string;
  tags: string | null;
  project: string | null;
  confidence: number;
  created_at: string;
  recall_count: number;
  suspect_of: number | null;
  liveness: Liveness;
}

export interface PrincipleRow {
  id: number;
  content: string;
  project: string | null;
  confidence: number;
  created_at: string;
  updated_at: string;
  liveness: Liveness;
}

export interface CostReport {
  totals: {
    sessions: number;
    total_in: number;
    total_out: number;
    total_wall_sec: number;
  };
  by_project: {
    project: string;
    sessions: number;
    tokens_in: number;
    tokens_out: number;
  }[];
  trend: { day: string; tokens_in: number; tokens_out: number; sessions: number }[];
}

export interface Slo {
  ok: boolean;
  slis: {
    name: string;
    value: number;
    target?: number;
    unit?: string;
    status?: string;
  }[];
}

// Journal events from /events/since: {type, ts, ...payload} where payload
// commonly carries id/project/preview (insight_saved) or pair ids (arena).
export interface EngineEvent {
  type?: string;
  ts?: string;
  id?: number;
  project?: string;
  preview?: string;
  summary?: string;
  [k: string]: unknown;
}

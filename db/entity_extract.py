"""Entity extraction for crag-anchor insights/principles."""
import re

# Common infrastructure service names used as entity-extraction hints. Extend
# this set for your own stack (it only affects how service entities are tagged
# for graph traversal — unknown services are simply not linked as `service`).
SERVICES = {"nginx", "cloudflare", "postgres", "redis", "flyway",
            "claude-code", "mcp", "prometheus", "grafana", "traefik", "caddy"}

ENV_VAR_BLOCKLIST = {"JSON", "HTTP", "HTTPS", "SQL", "FROM", "WHERE",
                     "BEGIN", "RSA", "AKIA", "USD", "EUR", "TODO", "FIXME",
                     "NOTE", "WARN", "INFO", "ERROR", "DEBUG", "TRUE", "FALSE",
                     "NULL", "NONE"}

VALID_TLDS = {"eu", "com", "net", "org", "io", "dev", "sh", "app", "ai", "tech"}

# HTTP/status codes to exclude from port extraction
_HTTP_STATUS_RE = re.compile(r"(?:HTTP|status|response|code)\s*[:/]?\s*\d*\s*$", re.I)

_PATTERNS = {
    # Port: preceded by ':' or 'port'/'listen'/'on port'. NOT followed by a dot (IP fragment)
    # or by '-\d' (line-range like :89-97). \b prevents partial-number matches.
    "port": re.compile(r"(?::|port\s+|listen\s+|on\s+port\s+)(\d{2,5})\b(?![-]\d|[.]\d)", re.I),
    "ip": re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b"),
    "domain": re.compile(r"\b([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)\b"),
    # Windows drive prefix captured when present (D:/x or D:\x) — stripping it
    # made windows paths look unix-absolute and thus never laptop-resolvable.
    "path": re.compile(r"((?:[A-Za-z]:)?[/\\][a-zA-Z0-9_./\\-]{4,})"),
    "service": re.compile(r"\b(" + "|".join(re.escape(s) for s in SERVICES) + r")\b", re.I),
    "classname": re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:Service|Controller|Repository|Manager|Handler|Client|Worker|Builder|Factory|Authenticator|Config))\b"),
    "env_var": re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b"),
    "file": re.compile(r"\b([a-zA-Z0-9_-]+\.(ps1|py|md|sql|ts|tsx|java|sh|yaml|yml|json|conf|toml|kts))\b"),
}

SKIP_IPS = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "1.1.1.1"}

# File extensions used to filter domains that look like filenames
_FILE_EXTENSIONS = re.compile(r"\.(ps1|py|md|sql|ts|tsx|java|sh|yaml|yml|json|conf|toml|kts|log|txt|cfg|ini|xml|html|css|js|rb|go|rs)$", re.I)


def extract_entities(content: str) -> list:
    """Extract named entities from a text string.

    Returns a list of dicts: [{"entity": ..., "entity_type": ..., "raw_match": ...}, ...]
    Deduplication is applied per (entity.lower(), entity_type) pair.
    """
    if not content:
        return []
    seen = set()
    out = []

    def add(entity, etype, raw):
        key = (entity.lower(), etype)
        if key not in seen:
            seen.add(key)
            out.append({"entity": entity, "entity_type": etype, "raw_match": raw})

    # --- ports ---
    for m in _PATTERNS["port"].finditer(content):
        port_str = m.group(1)
        port_num = int(port_str)
        # Skip HTTP status-code range (100-599) when preceded by HTTP/status context
        if 100 <= port_num <= 599:
            prefix = content[max(0, m.start() - 20):m.start()]
            if re.search(r"(HTTP|status|response|code)\s*[:/]?\s*$", prefix, re.I):
                continue
        if 1 <= port_num <= 65535:
            add(port_str, "port", m.group(0))

    # --- IPs ---
    for m in _PATTERNS["ip"].finditer(content):
        ip = m.group(1)
        if ip in SKIP_IPS:
            continue
        octets = ip.split(".")
        if all(0 <= int(o) <= 255 for o in octets):
            add(ip, "ip", ip)

    # --- domains ---
    for m in _PATTERNS["domain"].finditer(content):
        d = m.group(1).lower()
        # Must have at least two labels (a.b)
        parts = d.split(".")
        if len(parts) < 2:
            continue
        tld = parts[-1]
        if tld not in VALID_TLDS:
            continue
        # Filter out filenames disguised as domains (engine.db, app.py)
        if _FILE_EXTENSIONS.search(d):
            continue
        add(d, "domain", m.group(0))

    # --- paths ---
    for m in _PATTERNS["path"].finditer(content):
        p = m.group(1).rstrip(".,;:)\"'")  # sentence punctuation, not path chars
        # Require at least 2 non-empty path segments
        segments = [s for s in p.replace("\\", "/").split("/") if s]
        if len(segments) < 1 or len(p) < 5:
            continue
        add(p, "path", p)

    # --- services ---
    for m in _PATTERNS["service"].finditer(content):
        add(m.group(1).lower(), "service", m.group(0))

    # --- classnames ---
    for m in _PATTERNS["classname"].finditer(content):
        add(m.group(1), "classname", m.group(0))

    # --- env vars ---
    for m in _PATTERNS["env_var"].finditer(content):
        v = m.group(1)
        if v in ENV_VAR_BLOCKLIST:
            continue
        # Require underscore to filter out plain acronyms like 'VPS', 'CPU'
        if "_" not in v:
            continue
        add(v, "env_var", v)

    # --- files ---
    for m in _PATTERNS["file"].finditer(content):
        add(m.group(1), "file", m.group(0))

    return out


# ===========================================================================
# Phase 25 — Grounded Memory: derive a re-runnable falsifier from an entity.
# The falsifier KIND is a function of entity_type (no per-insight authoring).
# Specs are READ-ONLY command TEMPLATES; the groundskeeper runs cheap ones and
# only FLAGS, and the agent (ground_check MCP) refines + runs them with judgment.
# kind taxonomy matches migration 023 falsifiers.kind:
#   endpoint | grep_config | path_exists | grep_symbol | query | none
# ===========================================================================

# Most-decisively-falsifiable first. Used to pick the PRIMARY falsifier when a
# claim links to several entities.
FALSIFIER_PRIORITY = ["ip", "domain", "path", "file", "service",
                      "classname", "env_var", "port"]

# ---------------------------------------------------------------------------
# Falsifier entity QUALITY GATES (WS5 residual fix, 2026-07-02).
# SCOPE: TIER-A ONLY. These deny-lists gate the mechanical Tier-A falsifier
# derivation path (_falsifier_entity_ok + falsifier_for). They are irrelevant
# to Tier-B: Tier-B recipes are LLM-authored from the whole claim proposition,
# so the noun-liveness garbage (k8s vocab, Java packages, third-party domains)
# cannot occur — the LLM writes a falsification_question, not an entity probe.
# Do NOT apply these gates to Tier-B authoring prompts; do NOT delete them.
#
# An entity's liveness must say something about the CLAIM's truth. Three
# classes of lexed "entities" fail that test and produced garbage falsifiers
# (k8s label vocab curl'd as domains, Java packages curl'd as URLs, Stripe's
# uptime "verifying" a webhook gotcha, dead-by-design hallucination examples):
# ---------------------------------------------------------------------------

# 1) Vocabulary/third-party domains — uptime unrelated to any claim of ours.
_FALSIFIER_DENY_DOMAINS = (
    # k8s vocabulary that lexes as domains (label prefixes, API groups)
    "kubernetes.io", "k8s.io",
    # well-known third-party infrastructure / registries / resolvers
    "stripe.com", "mailgun.org", "cfargotunnel.com", "github.com",
    "cloudflare.com", "letsencrypt.org", "docker.io", "ghcr.io",
    "aquasec.com", "nist.gov", "infomaniak.com", "google.com",
    "anthropic.com", "githubusercontent.com",
)

# 2) Reverse-package names (Java/Go import paths) lex as domains because they
#    END in a TLD-ish token — but they START with one (com./org./net.), which
#    real hostnames never do in this corpus.
_REVERSE_PKG_RE = re.compile(r"^(?:com|org|net|edu|gov)\.[a-z]")

# 3) Private/reserved/public-resolver IPs — reachability is meaningless.
_DENY_IP_RE = re.compile(
    r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|127\.|169\.254\.|0\.|255\.)"
)
_DENY_IP_EXACT = {"8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9",
                  "185.12.64.1", "185.12.64.2"}  # public + Hetzner resolvers

# 4) Negation context — claims ABOUT nonexistent/hallucinated endpoints must
#    not get "is it alive?" falsifiers (a dead endpoint PROVES those claims).
_NEGATION_MARKERS = ("doesn't exist", "does not exist", "not exist", "no such",
                     "hallucinat", "invented", "never existed", "fake url",
                     "wrong url", "dead url", "is not a real")


def _falsifier_entity_ok(entity_type: str, value: str, content: str) -> bool:
    """Quality gate: would this entity's falsifier actually test the claim?"""
    v = (value or "").strip().lower()
    if not v:
        return False
    if entity_type == "domain":
        if _REVERSE_PKG_RE.match(v):
            return False
        if any(v == d or v.endswith("." + d) for d in _FALSIFIER_DENY_DOMAINS):
            return False
    if entity_type == "ip":
        if v in _DENY_IP_EXACT or _DENY_IP_RE.match(v):
            return False
    if entity_type == "path":
        # URL/label FRAGMENTS lex as paths ('app.kubernetes.io/component' →
        # '/component', 'https://x.eu/oauth2' → '/oauth2'). Real filesystem
        # paths are never glued to a preceding word character.
        if v.startswith("//"):
            return False
        idx = content.lower().find(v)
        if idx > 0 and content[idx - 1].isalnum():
            return False
        # single generic segment without extension (/api, /component) proves nothing
        if v.count("/") == 1 and "." not in v.rsplit("/", 1)[-1]:
            return False
    if entity_type in ("domain", "ip"):
        # negation window: ±120 chars around the first mention
        low = content.lower()
        idx = low.find(v)
        if idx != -1:
            window = low[max(0, idx - 120): idx + len(v) + 120]
            if any(m in window for m in _NEGATION_MARKERS):
                return False
    return True

# Repo-content globs the grep-based falsifiers scan (kept generic + read-only).
_CONFIG_GLOBS = "docker-compose*.yml *.conf *.yaml *.yml .env* nginx*"
_CODE_GLOBS = "--include=*.py --include=*.ts --include=*.tsx --include=*.java --include=*.ps1 --include=*.sh"


def falsifier_for(entity_type: str, value: str) -> dict:
    """Return {kind, spec} — a read-only check derived from one entity.

    The spec is a portable template; absolute knowledge (which port pairs with
    an IP, which repo a path lives in) is the agent's job at ground_check time.
    A failing cheap check only FLAGS (grounding_due) — it never demotes.
    """
    v = (value or "").strip()
    if not v:
        return {"kind": "none", "spec": None}

    if entity_type == "ip":
        return {"kind": "endpoint",
                "spec": f"curl -sf --connect-timeout 5 http://{v}/ "
                        f"# IP reachability; agent fills the port from the claim"}
    if entity_type == "domain":
        return {"kind": "endpoint",
                "spec": f"curl -sf --connect-timeout 6 https://{v}/v1/health || "
                        f"curl -sf --connect-timeout 6 https://{v}/"}
    if entity_type == "service":
        # A bare service name is NOT laptop-checkable without its host+port, and
        # a comment spec runs as a no-op (bash exit 0 = fake PASS). Emit no
        # falsifier — the agent may still health-check it at ground_check time
        # using principle #146, but the automated layer must not pretend it can.
        return {"kind": "none", "spec": None}
    if entity_type == "port":
        return {"kind": "grep_config",
                "spec": f"grep -rn ':{v}\\b' {_CONFIG_GLOBS} 2>/dev/null"}
    if entity_type == "path":
        # absolute paths are decisively checkable; relative → grep
        if v.startswith("/") or (len(v) > 2 and v[1] == ":"):
            return {"kind": "path_exists", "spec": f"test -e '{v}' && echo PRESENT || echo MISSING"}
        return {"kind": "grep_symbol", "spec": f"grep -rn '{v}' . 2>/dev/null | head"}
    if entity_type == "file":
        return {"kind": "path_exists",
                "spec": f"find . -name '{v}' -not -path '*/node_modules/*' 2>/dev/null | head"}
    if entity_type == "classname":
        return {"kind": "grep_symbol",
                "spec": f"grep -rn '\\b{v}\\b' {_CODE_GLOBS} 2>/dev/null | head"}
    if entity_type == "env_var":
        return {"kind": "grep_symbol",
                "spec": f"grep -rn '\\b{v}\\b' . 2>/dev/null | head"}
    return {"kind": "none", "spec": None}


# Doomed-by-design literals: storing the VALUE is the bug (it drifts every
# build/refactor). STRONG signals beat topology (a line-number is doomed even in
# an infra claim); the WEAK semver signal yields to topology because bare
# \d+.\d+.\d+ collides with IPv4 (203.0.113.10 must classify as topology, not obs).
_OBS_STRONG = [
    re.compile(r"\bcustom\.\d+\b"),                    # claudex 2.1.92-custom.8
    re.compile(r"\b[0-9a-f]{40,64}\b", re.I),          # git/sha256 hashes
    re.compile(r"[\w./-]+\.(?:py|ts|tsx|java|ps1|sh|sql):\d{1,5}\b"),  # file.py:467 line refs
    re.compile(r"\b\d[\d,]{2,}\s+(?:rows|tokens|insights|links|bytes|refs|commits)\b", re.I),  # counts
]
_OBS_WEAK = [
    re.compile(r"\bv\d+\.\d+\.\d+\b"),                 # explicit v1.2.3
    re.compile(r"\b\d+\.\d+\.\d+\b"),                  # bare semver (also matches IPv4)
]


def classify_volatility(content: str) -> str:
    """Best-effort volatility class for a claim's content.

    Returns 'observation' | 'topology' | 'invariant' | None (unclassified).
    Cheap, regex-only — used at write-time (25-D) and by the groundskeeper.
    Precedence: invariant > strong-observation > topology > weak-observation.
    """
    if not content:
        return None
    low = content.lower()
    # invariant: safety/architectural absolutes that should rarely re-ground
    if any(k in low for k in ("breathing cord", "never kill", "never push", "never run",
                              "safety rule", "must never", "always use")):
        return "invariant"
    # strong observation: line-refs / hashes / build-tags / counts always drift
    if any(p.search(content) for p in _OBS_STRONG):
        return "observation"
    # topology: an infra entity that moves on migration (host/service moves)
    ents = {e["entity_type"] for e in extract_entities(content)}
    if ents & {"ip", "domain", "port", "path", "service"}:
        return "topology"
    # weak observation: a version literal with no infra anchor
    if any(p.search(content) for p in _OBS_WEAK):
        return "observation"
    return None


def derive_falsifier(content: str) -> dict:
    """Pick the strongest entity in content and derive its falsifier.

    Returns {kind, spec, entity, entity_type} or {kind:'none',...} if no
    falsifiable entity is present (e.g. operator-preference / user-context claims).
    """
    ents = extract_entities(content)
    by_type: dict[str, list[str]] = {}
    for e in ents:
        by_type.setdefault(e["entity_type"], []).append(e["entity"])
    for etype in FALSIFIER_PRIORITY:
        # quality gate (WS5): skip vocabulary/third-party/negated entities and
        # fall through to the next candidate of the same or lower priority.
        for value in by_type.get(etype, []):
            if not _falsifier_entity_ok(etype, value, content):
                continue
            f = falsifier_for(etype, value)
            f["entity"] = value
            f["entity_type"] = etype
            return f
    return {"kind": "none", "spec": None, "entity": None, "entity_type": None}

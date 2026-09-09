#!/usr/bin/env python3
"""
Memory retrieval layer over the compiled KB.

Two responsibilities, deliberately kept in one module so the eager hook and the
(future) lazy recall tool share ONE index and ONE scoring function:

1. build_index()  -> scan compiled/topics/*.md (+ their source pages for project
                     scope), extract the settled-facts section and match keys,
                     write state/memory_index.json.
2. retrieve()     -> given a query + project, return scored matches with a TIER
                     (high / moderate / none) so callers can decide eager-inject
                     vs. advertise vs. stay silent.

Design constraints (established with the operator):
- Retrieval, not residence: nothing lives in context by default; only matched,
  settled facts are fetched.
- Inject the CONSOLIDATED layer (topic "Key points"), never raw corrections,
  so supersession is already resolved and we don't re-expose stale facts.
- Project-scoped: a fact from one client must not surface in another's session.
  Project is NOT on the manifest; it lives in the identity block of the source
  pages, so we parse it from there and attach the union to each topic.
- The HIGH band auto-injects as directives. The MODERATE band only advertises
  (handled by the hook, not here). Thresholds are conservative by default and
  meant to be tuned from the recurring-correction signal, not guessed once.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
COMPILED_TOPICS_DIR = BASE_DIR / 'compiled' / 'topics'
COMPILED_SOURCES_DIR = BASE_DIR / 'compiled' / 'sources'
STATE_DIR = BASE_DIR / 'state'
INDEX_PATH = STATE_DIR / 'memory_index.json'

# --- Tiering thresholds. CONSERVATIVE by design. -------------------------------
# When unsure, prefer MODERATE (advertise) over HIGH (assert): a wrongly-offered
# pointer is ignored at near-zero cost; a wrongly-injected fact mis-steers
# invisibly. These are the dials you tune from the tier log + recurrence signal.
HIGH_CONFIDENCE = 0.72
MODERATE_CONFIDENCE = 0.40

# Generic slug words that carry no topic identity — excluded when computing how
# much of a topic's DISTINCTIVE name the query hit, so "nimbus auth" scores as a
# full hit on "nimbus-auth-flow" rather than 2/3.
SLUG_FILLER = {
    'flow', 'system', 'notes', 'note', 'setup', 'config', 'configuration',
    'integration', 'overview', 'general', 'misc', 'workflow', 'process',
    'implementation', 'guide', 'docs', 'doc', 'api',
}

# Stopwords kept tiny on purpose — we WANT distinctive technical tokens
# (sessionHash, mTLS, OAuth2) to carry the match.
STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be',
    'to', 'of', 'in', 'on', 'for', 'with', 'how', 'do', 'i', 'we', 'it', 'this',
    'that', 'should', 'can', 'use', 'using', 'add', 'set', 'get', 'my', 'our',
    'you', 'me', 'please', 'help', 'need', 'want', 'what', 'which', 'when',
}

MANIFEST_PATH = STATE_DIR / 'manifest.json'

# Values that mean "no specific project" — treated as global (surfaces everywhere).
_GLOBAL_PROJECT_SENTINELS = {'', 'none', 'global', 'unknown'}

CONFIG_DIR = BASE_DIR / 'config'
TUNING_PATH = CONFIG_DIR / 'memory_tuning.yaml'

# Hardcoded fallbacks. If config/memory_tuning.yaml is missing or unparseable the
# hook keeps working at exactly today's behavior (fail-open). The thresholds
# mirror the module constants above so there is one source of truth.
DEFAULT_TUNING = {
    'thresholds': {'high': HIGH_CONFIDENCE, 'moderate': MODERATE_CONFIDENCE},
    'high_ineligible': [],
    'outcome_demoted': [],
    'project_catch_all_tokens': [],
    'auto_tune': {
        'enabled': True,
        'window_days': 14,
        'min_samples': 3,
        'cross_project_min': 3,
        'threshold_step': 0.01,
        'threshold_ceiling': 0.85,
        'false_high_rate_trigger': 0.30,
    },
}


def _coerce_scalar(val: str):
    v = val.strip().strip('`"\'')
    low = v.lower()
    if low in ('true', 'false'):
        return low == 'true'
    if re.fullmatch(r'-?\d+', v):
        return int(v)
    if re.fullmatch(r'-?\d*\.\d+', v):
        return float(v)
    return v


def _parse_tuning_yaml(text: str) -> dict:
    """Stdlib parser for the FLAT two-level shape of memory_tuning.yaml ONLY:
    a top-level 'key:' followed by indented 'k: v' scalars OR '- item' list lines.
    No third-party dep so the system-python3 hook can read it. The auditor writes
    this file surgically, so the shape stays exactly as authored."""
    out: dict = {}
    cur_key = None
    for raw in text.splitlines():
        line = '' if raw.lstrip().startswith('#') else raw.split('#', 1)[0].rstrip()
        if not line.strip():
            continue
        m = re.match(r'^([A-Za-z0-9_]+):\s*(.*)$', line)
        if m and not line[0].isspace():
            cur_key, val = m.group(1), m.group(2).strip()
            out[cur_key] = _coerce_scalar(val) if val else None
            continue
        m = re.match(r'^\s+([A-Za-z0-9_]+):\s*(.+)$', line)
        if m and cur_key is not None:
            if not isinstance(out.get(cur_key), dict):
                out[cur_key] = {}
            out[cur_key][m.group(1)] = _coerce_scalar(m.group(2))
            continue
        m = re.match(r'^\s*-\s+(.+)$', line)
        if m and cur_key is not None:
            if not isinstance(out.get(cur_key), list):
                out[cur_key] = []
            out[cur_key].append(_coerce_scalar(m.group(1)))
    return out


def _deep_merge(base: dict, over: dict) -> dict:
    merged = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
              for k, v in base.items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        elif v is not None:
            merged[k] = v
    return merged


def load_tuning() -> dict:
    """Tuning knobs merged over DEFAULT_TUNING. Fail-open: any error -> defaults."""
    try:
        return _deep_merge(DEFAULT_TUNING, _parse_tuning_yaml(read_text(TUNING_PATH)))
    except Exception:
        return _deep_merge(DEFAULT_TUNING, {})


# ---------- parsing helpers ----------------------------------------------------

def read_text(path: Path) -> str:
    return path.read_text(errors='replace')


def _unquote(val: str) -> str:
    """Strip one pair of surrounding quotes. kb.py writes frontmatter via
    yaml.safe_dump, which single-quotes scalars containing ':' — e.g. ISO
    timestamps like compiled_at. This line-based parser would otherwise keep the
    quotes, so the value reads back as "'2026-...'" and never parses as a date —
    which is exactly why a freshness/staleness gate could never be wired."""
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-ish frontmatter split. We only need a few scalar keys and
    the source_ids list, so we avoid a yaml dep here to keep the hook light."""
    meta: dict = {}
    body = text
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            fm = text[3:end].strip()
            body = text[end + 4:].lstrip('\n')
            cur_key = None
            for line in fm.splitlines():
                if re.match(r'^\s*-\s+', line) and cur_key:
                    meta.setdefault(cur_key, [])
                    if isinstance(meta[cur_key], list):
                        meta[cur_key].append(_unquote(line.split('-', 1)[1].strip()))
                    continue
                m = re.match(r'^([A-Za-z0-9_]+):\s*(.*)$', line)
                if m:
                    cur_key, val = m.group(1), _unquote(m.group(2).strip())
                    if val == '':
                        meta[cur_key] = []
                    else:
                        meta[cur_key] = val
    return meta, body


# List-item markers seen in compiled topics. '-'/'*' are the markdown norm; the
# Unicode bullets ('•' especially) and numbered lists ('1.'/'2)') are what the
# small Ollama models emit unprompted — all of which silently produced
# key_points=[] and dropped otherwise-good topics from the index.
_LIST_MARKER_RE = re.compile(r'^(?:[-*•‣·▪◦–]\s*|\d+[.)]\s+)')


def extract_section(body: str, header: str) -> list[str]:
    """Return bullet lines under a section header. Recognizes the SAME header
    shapes as kb.extract_bullets — '## '/'### '/'#### ' and '**bold**', optional
    trailing colon — so the index doesn't silently drop topics whose 'Key points'
    was emitted at ### or in bold (a stricter '## '-only match dropped 6)."""
    name = header.strip()
    header_pat = re.compile(
        rf'(?mi)^\s*(?:#{{2,4}}\s+|\*\*\s*){re.escape(name)}\s*\*?\*?\s*:?\s*$')
    m = header_pat.search(body)
    if not m:
        return []
    rest = body[m.end():]
    nxt = re.search(r'(?m)^\s*(?:#{2,4}\s+|\*\*[A-Z])', rest)
    block = rest[:nxt.start()] if nxt else rest
    out: list[str] = []
    for line in block.splitlines():
        s = line.strip()
        # Accept Unicode bullets and numbered lists, not just '-'/'*': weak local
        # models routinely emit '•' or '1.' which silently produced key_points=[]
        # and dropped otherwise-good topics from the index.
        mk = _LIST_MARKER_RE.match(s)
        if mk:
            out.append(s[mk.end():].strip())
    return out


def _stem(tok: str) -> str:
    """Cheap suffix folding so 'redirect'/'redirects', 'hash'/'hashes',
    'mapping'/'map' collapse. Not linguistically correct — just enough to stop
    exact-match from missing the singular/plural and verb-form variants that
    natural phrasing produces constantly. Deliberately conservative: short tokens
    and acronym-like tokens (sessionHash lowercased -> sessionhash) are left alone."""
    if len(tok) <= 4:
        return tok
    for suf in ('ies',):
        if tok.endswith(suf) and len(tok) > len(suf) + 2:
            return tok[:-len(suf)] + 'y'
    for suf in ('ing', 'ed', 'es', 's'):
        if tok.endswith(suf) and len(tok) > len(suf) + 2:
            return tok[:-len(suf)]
    return tok


def tokenize(text: str) -> set[str]:
    toks = re.findall(r'[A-Za-z0-9_]+', text.lower())
    return {_stem(t) for t in toks if t not in STOPWORDS and len(t) > 1}


# ---------- project scope (read from the manifest, the source of truth) --------

_manifest_project_cache: dict[str, str | None] | None = None


def _manifest_projects() -> dict[str, str | None]:
    """source_id -> project tag from state/manifest.json (cached per process).

    Project lives on the manifest source record (set by a one-time Opus
    classification / future ingest step), not in the compiled files, because
    compiled/sources/*.md are regenerated by summarization and would lose any
    inline tag. Missing/global/unknown all mean 'no scope' (surfaces everywhere).
    """
    global _manifest_project_cache
    if _manifest_project_cache is None:
        try:
            m = json.loads(read_text(MANIFEST_PATH))
            _manifest_project_cache = {s['source_id']: s.get('project') for s in m.get('sources', [])}
        except Exception:
            _manifest_project_cache = {}
    return _manifest_project_cache


def project_of_source(source_id: str) -> str | None:
    val = _manifest_projects().get(source_id)
    if val is None:
        return None
    val = str(val).strip().strip('`"\'')
    if val.lower() in _GLOBAL_PROJECT_SENTINELS:
        return None
    return val or None


# ---------- index build --------------------------------------------------------

def build_index() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    if COMPILED_TOPICS_DIR.exists():
        for path in sorted(COMPILED_TOPICS_DIR.glob('*.md')):
            text = read_text(path)
            meta, body = split_frontmatter(text)
            slug = meta.get('topic_slug') or path.stem
            display = meta.get('topic') or slug.replace('-', ' ')
            key_points = extract_section(body, 'Key points')
            if not key_points:
                # No settled facts to inject — skip; contradictions/open-questions
                # are not directive-grade and must never be eager-injected.
                continue
            source_ids = meta.get('source_ids') or []
            if isinstance(source_ids, str):
                source_ids = [source_ids]
            projects = sorted({p for sid in source_ids if (p := project_of_source(sid))})

            # Match surface: slug tokens + display tokens + tokens drawn from the
            # facts themselves, so distinctive terms inside Key points (sessionHash)
            # become matchable even if absent from the slug.
            match_tokens = tokenize(slug.replace('-', ' '))
            match_tokens |= tokenize(display)
            for kp in key_points:
                match_tokens |= tokenize(kp)

            entries.append({
                'slug': slug,
                'display': display,
                'projects': projects,           # empty == unscoped/global
                'key_points': key_points,
                'match_tokens': sorted(match_tokens),
                'slug_tokens': sorted(tokenize(slug.replace('-', ' ')) | tokenize(display)),
                'compiled_at': meta.get('compiled_at'),
                'path': str(path.relative_to(BASE_DIR)),
            })
    index = {'entries': entries, 'built_from': str(COMPILED_TOPICS_DIR.relative_to(BASE_DIR))}
    INDEX_PATH.write_text(json.dumps(index, indent=2))
    return {'topics_indexed': len(entries), 'index_path': str(INDEX_PATH.relative_to(BASE_DIR))}


def load_index() -> dict:
    if not INDEX_PATH.exists():
        return {'entries': []}
    return json.loads(read_text(INDEX_PATH))


# ---------- scoring + retrieval ------------------------------------------------

def _is_catch_all(entry: dict, distinctive_slug: set[str], tuning: dict) -> bool:
    """A slug behaves as a catch-all (must not reach HIGH) when it is explicitly
    blocklisted, or when its distinctive name is a single token equal to a known
    project name. Such a slug scores a perfect slug-hit on any mention of that
    one word, so a narrow note force-injects on unrelated turns.

    `high_ineligible` is the PERMANENT list (manual catch-alls + synthetic-proven
    false-firers). `outcome_demoted` is the REVERSIBLE list the nightly auditor
    rewrites each run from real-traffic outcomes — a slug leaves it automatically
    once its misfires age out of the window, so demotion is never a one-way latch."""
    demoted = set(tuning.get('high_ineligible', [])) | set(tuning.get('outcome_demoted', []))
    if entry.get('slug') in demoted:
        return True
    catch_tokens: set[str] = set()
    for t in tuning.get('project_catch_all_tokens', []):
        catch_tokens |= tokenize(str(t).replace('-', ' '))
    return len(distinctive_slug) == 1 and next(iter(distinctive_slug)) in catch_tokens


def score_entry(query_tokens: set[str], entry: dict, tuning: dict | None = None) -> float:
    """Confidence in [0,1].

    Judged on how strongly the query hits the topic's DISTINCTIVE tokens, not on
    how much of the (verb-noise-laden) query is covered. Natural phrasing like
    "how do I wire up the nimbus auth token call" must not be penalized for the
    filler words no topic could ever match, nor for missing slug-filler like
    "flow". The signal is: did the user name this topic's identifying terms?
    """
    if tuning is None:
        tuning = load_tuning()
    high = tuning['thresholds']['high']
    moderate = tuning['thresholds']['moderate']
    if not query_tokens:
        return 0.0
    match_tokens = set(entry['match_tokens'])
    slug_tokens = set(entry['slug_tokens'])

    overlap = query_tokens & match_tokens
    if not overlap:
        return 0.0

    slug_overlap = query_tokens & slug_tokens

    # Component 1: how many of the topic's slug/title terms the user named,
    # ignoring ultra-generic slug filler so "flow"/"system"/"notes" don't dilute.
    distinctive_slug = {t for t in slug_tokens if t not in SLUG_FILLER}
    if distinctive_slug:
        slug_hit_ratio = len(query_tokens & distinctive_slug) / len(distinctive_slug)
    else:
        slug_hit_ratio = len(slug_overlap) / max(1, len(slug_tokens))

    # Component 2: did the query also land on facts inside the topic (beyond the
    # slug)? A single distinctive fact token (e.g. "sessionhash") is meaningful,
    # so the first hit counts heavily; further hits add diminishing confidence.
    fact_only = match_tokens - slug_tokens
    fact_hits = len(query_tokens & fact_only)
    if fact_hits >= 1:
        fact_bonus = min(1.0, 0.6 + 0.2 * (fact_hits - 1))
    else:
        fact_bonus = 0.0

    # Blend: slug naming dominates (it means the query is ABOUT the topic);
    # fact hits add confidence on top.
    score = 0.75 * slug_hit_ratio + 0.25 * fact_bonus

    distinctive_hits = len(query_tokens & distinctive_slug)

    # Precision guard (added after supervised review caught false-HIGHs): a HIGH
    # injection must rest on more than a single distinctive slug token. 53/845
    # topics reduce to ONE distinctive token after filler-stripping (e.g. "model"
    # in "Config Model Implementation", "monitor", "api", "automation"); without
    # this, any incidental mention of that one common word scores a perfect
    # slug_hit_ratio and force-injects the topic. Require either >=2 distinctive
    # slug hits OR 1 distinctive hit with GENUINE topical engagement (>=2 fact
    # hits); otherwise cap below HIGH (it can still ADVERTISE in the moderate band).
    # The single-fact bar was too weak — one incidental fact token (e.g. a shared
    # "status"/"backlog") on slugs like api-changes/config-api-migration force-
    # injected off-topic notes on real user prompts (2026-06-29 misfires).
    if distinctive_hits <= 1 and fact_hits < 2:
        score = min(score, high - 0.01)

    if not distinctive_hits:
        if fact_hits >= 1:
            score = max(moderate, min(score, high - 0.01))
        else:
            score = min(score, moderate - 0.01)

    # Catch-all demotion: a blocklisted or bare-project-name slug can never reach
    # HIGH (it would inject a narrow note on any incidental mention). It may still
    # ADVERTISE in the moderate band.
    if _is_catch_all(entry, distinctive_slug, tuning):
        score = min(score, high - 0.01)

    return round(min(1.0, score), 4)


def tier_for(score: float, tuning: dict | None = None) -> str:
    if tuning is None:
        tuning = load_tuning()
    if score >= tuning['thresholds']['high']:
        return 'high'
    if score >= tuning['thresholds']['moderate']:
        return 'moderate'
    return 'none'


def retrieval_mode() -> str:
    """lexical (default) | embedding | hybrid. Read at call time so the flag can
    be flipped per-process (eval A/B) without restarting anything. Defaulting to
    lexical means the live hook is unchanged until the flag is deliberately set."""
    return os.environ.get('KB_RETRIEVAL', 'lexical').strip().lower()


def _embed_thresholds(mode: str, tuning: dict) -> tuple[float, float]:
    """Embedding cosine lives in a different, compressed range than the lexical
    0-1 score, so it needs its own thresholds (env-overridable)."""
    if mode == 'embedding':
        # Calibrated 2026-06-28 on the eval set: off-domain negatives cap at 0.559,
        # so 0.58 is the lowest score-floor that holds them silent. PROVISIONAL —
        # 22 cases / 6 negatives; widen the golden set before trusting in production.
        return (float(os.environ.get('KB_EMBED_HIGH', '0.58')),
                float(os.environ.get('KB_EMBED_MODERATE', '0.52')))
    return (float(os.environ.get('KB_HYBRID_HIGH', str(tuning['thresholds']['high']))),
            float(os.environ.get('KB_HYBRID_MODERATE', str(tuning['thresholds']['moderate']))))


def _embed_tiers(scored: list, mode: str, tuning: dict) -> list:
    """RANK-CAPPED tiering for embedding-based scores: HIGH is reserved for the
    top-K by rank that also clear the score floor. An absolute cosine floor alone
    over-fires — on an on-domain query dozens of sibling topics look 'similar'
    (confirmed in shadow: embedding marked HIGH on 64% of real turns). HIGH thus
    means 'a top candidate', not merely 'above 0.58'. Used both for plain
    embedding/hybrid modes and as the cheap path when the rerank gate skips the
    judge (rerank scores ARE embedding cosines, so pass mode='embedding')."""
    hi, mod = _embed_thresholds(mode, tuning)
    topk = int(os.environ.get('KB_EMBED_TOPK', '3'))
    return [
        (s, ('high' if (rank < topk and s >= hi) else 'moderate' if s >= mod else 'none'), entry)
        for rank, (s, _, entry) in enumerate(scored)
    ]


def _should_rerank(scored: list, tuning: dict) -> bool:
    """Decide whether to spend the LLM judge. Always require a plausibly-relevant
    top candidate (top1 >= floor) — else there's nothing to inject and nothing to
    judge.

    DEFAULT: rerank every would-inject turn. The 'ambiguity gate' (skip the judge
    when one candidate dominates, gap >= KB_RERANK_GAP) was MEASURED to hurt — on
    the golden set it dropped hit@1 50% -> 38%, because embedding is often
    confidently WRONG (its top-1 dominates the gap but is the wrong topic, and the
    judge's whole job is to override exactly those). So the gap gate is an explicit,
    cost-only OPT-IN: it engages only when KB_RERANK_GAP is set. KB_RERANK_ALWAYS=1
    forces reranking even if a gap is set (used by shadow mode)."""
    if not scored:
        return False
    floor = float(os.environ.get('KB_RERANK_FLOOR', str(_embed_thresholds('embedding', tuning)[1])))
    top1 = scored[0][0]
    if top1 < floor:
        return False
    if os.environ.get('KB_RERANK_ALWAYS', '0') != '0':
        return True
    gap_threshold = os.environ.get('KB_RERANK_GAP')
    if gap_threshold is None:
        return True                      # no gate set -> always rerank (best precision)
    gap = top1 - (scored[1][0] if len(scored) > 1 else 0.0)
    return gap < float(gap_threshold)


def _rerank_tiers(query: str, scored: list, tuning: dict) -> tuple[list, bool]:
    """Precision stage: hand the top-K embedding candidates to a local LLM judge
    and let it pick the one that actually answers the query (or none). The winner
    becomes the single HIGH match (moved to rank 0); the rest can only advertise.

    Returns (tiered, ok). ok=False means the judge CALL failed — timeout or
    transport — so NO verdict exists: the caller must fall back to the lexical
    scorer with provenance (Task 9 bounded-fallback invariant, 2026-07-29). A
    verdict of "none relevant" is ok=True with advertise-only tiering — that is
    certified rerank semantics, not a failure. The two used to collapse into one
    branch, which made a timed-out judge indistinguishable from a real verdict."""
    k = int(os.environ.get('KB_RERANK_K', '10'))
    hi, mod = _embed_thresholds('embedding', tuning)
    cands = scored[:k]
    try:
        import reranker  # noqa: PLC0415
        winner = reranker.rerank(query, [
            {'slug': e['slug'], 'display': e['display'], 'key_points': e['key_points']}
            for _s, _t, e in cands
        ])
    except Exception:
        return scored, False             # call failed — no verdict happened

    if winner is None:
        # judge VERDICT: none relevant -> nothing injectable, only advertise
        return [(s, ('moderate' if s >= mod else 'none'), e) for s, _t, e in scored], True

    # The judge picks WHICH candidate; embedding's confidence still gates WHETHER it
    # injects. A winner below the HIGH floor is the judge grabbing a weak candidate
    # on a conversational non-query — promote it to top, but do NOT make it HIGH.
    # (Measured: without this floor rerank's false-HIGH on real non-queries was 47%,
    # worse than embedding's 33%.)
    ws = scored[winner][0]
    win_tier = 'high' if ws >= hi else ('moderate' if ws >= mod else 'none')
    tiered = [(s, (win_tier if i == winner else 'moderate' if s >= mod else 'none'), e)
              for i, (s, _t, e) in enumerate(scored)]
    chosen = tiered.pop(winner)          # surface the judged-best as matches[0]
    tiered.insert(0, chosen)
    return tiered, True


def retrieve(query: str, project: str | None = None, max_facts: int = 6,
             tuning: dict | None = None, limit: int = 50) -> dict:
    """Return tiered matches. Caller (hook) decides inject vs advertise vs silent.

    Project filter: keep entries that are global (no project) OR match the
    session project. Cross-project facts are excluded — both noise and a
    correctness risk (clients differ).

    Scorer is selected by KB_RETRIEVAL (lexical|embedding|hybrid). embedding and
    hybrid need the topic-embedding cache (built offline); if it is missing or
    the embed call fails, this falls back to lexical so retrieval never breaks."""
    if tuning is None:
        tuning = load_tuning()
    index = load_index()
    qtokens = tokenize(query)
    proj_norm = (project or '').strip().lower()

    mode = retrieval_mode()
    er = qvec = None
    topic_vecs: dict = {}
    if mode in ('embedding', 'hybrid', 'rerank'):
        try:
            import embedding_rescorer as er  # noqa: PLC0415 (lazy: hook stays lexical-only)
            topic_vecs = er.load_topic_vectors()
            if not topic_vecs:
                mode = 'lexical'             # no cache -> fail safe
            else:
                qvec = er.embed(query)
        except Exception:
            mode = 'lexical'                 # ollama down / model missing -> fail safe

    # Version pin for Task 10: the ATTEMPTED config, kept even when the judge call
    # fails and lexical runs — the row's full story is "attempted rerank-kXfY, fell
    # back". The facts default mirrors reranker.RERANK_FACTS's ('3'). The judge
    # MODEL is always part of the string (2026-08-01): new model = new scorer, and
    # k10f3-on-3B must never masquerade as the 7B's or haiku's certification.
    # (Rows from the 7B era say bare 'rerank-kXfY' — 7B implicit, append-only.)
    if mode == 'rerank':
        import reranker  # noqa: PLC0415 (stdlib-only module)
        judge = (reranker.RERANK_CLOUD_MODEL_ID
                 if reranker.rerank_backend() == 'claude'
                 else os.environ.get('KB_RERANK_MODEL', reranker.RERANK_MODEL))
        retrieval_config = (f"rerank-k{int(os.environ.get('KB_RERANK_K', '10'))}"
                            f"f{int(os.environ.get('KB_RERANK_FACTS', '3'))}@{judge}")
    else:
        retrieval_config = mode

    scored = []
    lex_scored = []   # rerank mode retains the lexical view for the timeout fallback
    for entry in index['entries']:
        projects = [p.lower() for p in entry.get('projects', [])]
        if projects and proj_norm and proj_norm not in projects:
            continue  # scoped to a different client
        lex = score_entry(qtokens, entry, tuning)
        if mode == 'lexical':
            s, tier = lex, tier_for(lex, tuning)
        else:
            vec = topic_vecs.get(entry['slug'])
            emb = max(0.0, er.cosine(qvec, vec)) if vec else 0.0
            # rerank scores on embedding (it just reorders the top-K afterwards)
            s, tier = (emb if mode in ('embedding', 'rerank') else 0.5 * lex + 0.5 * emb), None
            if mode == 'rerank' and lex > 0:
                lex_scored.append((lex, tier_for(lex, tuning), entry))
        if s <= 0:
            continue
        scored.append((s, tier, entry))

    scored.sort(key=lambda x: x[0], reverse=True)

    rerank_timeout = False
    if mode == 'rerank':
        # Ambiguity-gated: only spend the LLM judge when it can change the outcome;
        # otherwise fall through to the cheap embedding rank-cap (rerank scores are
        # cosines, so tier them as 'embedding').
        if _should_rerank(scored, tuning):
            tiered, ok = _rerank_tiers(query, scored, tuning)
            if ok:
                scored = tiered
            else:
                # Judge call FAILED (timeout/transport) — no verdict exists. Bounded
                # fallback (Task 9, 2026-07-29): inject the LEXICAL result instead
                # and report the scorer that ACTUALLY ran, so no would-inject turn is
                # dropped and no row misreports its scorer. The injection-log row
                # carries mode + rerank_timeout as provenance.
                lex_scored.sort(key=lambda x: x[0], reverse=True)
                scored = lex_scored
                mode = 'lexical'
                rerank_timeout = True
        else:
            scored = _embed_tiers(scored, 'embedding', tuning)
    elif mode != 'lexical':
        scored = _embed_tiers(scored, mode, tuning)

    results = []
    for s, tier, entry in scored[:limit]:
        results.append({
            'slug': entry['slug'],
            'display': entry['display'],
            'score': s,
            'tier': tier,
            'projects': entry.get('projects', []),
            'key_points': entry['key_points'][:max_facts],
            'path': entry['path'],
        })

    top = results[0] if results else None
    return {
        'query': query,
        'project': project,
        'mode': mode,                       # the scorer that ACTUALLY produced this
        'retrieval_config': retrieval_config,   # the ATTEMPTED config (Task 10 pin)
        'rerank_timeout': rerank_timeout,   # True = judge call failed, lexical ran
        'top_tier': top['tier'] if top else 'none',
        'matches': results,
    }


# ---------- injection formatting ----------------------------------------------

def format_directive(match: dict, max_facts: int = 5) -> str:
    """HIGH-band payload: terse facts from the KB, provenance stripped.
    Framed as context to verify against the live code/task, not as orders."""
    facts = match['key_points'][:max_facts]
    lines = [f"[memory: {match['display']}] possibly-relevant context from your KB (auto-summarized — may be stale or out of scope; verify against the live code/task before relying on it):"]
    lines.extend(f"- {f}" for f in facts)
    return '\n'.join(lines)


def format_pointer(matches: list[dict]) -> str:
    """MODERATE-band breadcrumb: advertises existence, asserts no fact, ~cheap.
    Converts 'model must realize it needs context' into 'model was told it can pull'."""
    names = ', '.join(f'"{m["display"]}"' for m in matches[:3])
    return (f"[memory: possibly-relevant notes exist for {names} "
            f"(moderate confidence). Call recall_memory with the topic if useful.]")


# ---------- cli ----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description='Build/query the KB memory index.')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('build', help='Rebuild the memory index from compiled/topics/')
    q = sub.add_parser('query', help='Query the index (debug)')
    q.add_argument('text')
    q.add_argument('--project', default=None)
    q.add_argument('--json', action='store_true')
    args = p.parse_args()

    if args.cmd == 'build':
        print(json.dumps(build_index(), indent=2))
    elif args.cmd == 'query':
        res = retrieve(args.text, project=args.project)
        if args.json:
            print(json.dumps(res, indent=2))
            return
        print(f"top_tier={res['top_tier']}  ({len(res['matches'])} match(es))")
        for m in res['matches']:
            print(f"  [{m['tier']:8}] {m['score']:.3f}  {m['display']}  projects={m['projects'] or '*'}")


if __name__ == '__main__':
    main()

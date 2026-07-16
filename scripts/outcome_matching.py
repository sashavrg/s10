#!/usr/bin/env python3
"""Shared v2 engagement/correction proxies (measurement instrument v2).

v1 (injection_outcome.compute_outcomes) scored `engaged` as ANY single probe token
appearing as a SUBSTRING anywhere in the concatenated assistant text ("api" matched
"therapist"), and `corrected` on slug tokens only. The 2026-07-02 instrument audit
found this too weak to carry the useful/harmful verdict. v2:

  * word-boundary matching — probe tokens must appear as whole tokens, and
  * a quorum — at least min(2, len(probe)) distinct probe tokens must match, and
  * audit trails — every verdict records WHICH tokens matched (`*_matched`), so
    hand-audits (label_sample.py) can check the proxy instead of trusting it.

Stdlib-only: imported by the SessionEnd hook path under the system python3.
Considered and cut: per-turn time attribution (injection ts is local-naive,
transcript ts is UTC — fragile join; revisit only if hand-labels demand it).
"""
from __future__ import annotations

import re

SCORER_VERSION = 2

_WORD_RE = re.compile(r'[a-z0-9]+')
# Tokens too generic to be evidence of topical engagement (mirrors injection_outcome v1).
_STOP = {
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'not', 'was', 'are',
    'has', 'have', 'you', 'your', 'use', 'used', 'using', 'via', 'per', 'set',
    'get', 'run', 'all', 'any', 'new', 'old', 'one', 'two', 'now', 'out',
}
_MATCHED_CAP = 10


def tokenize(text: str | None) -> set[str]:
    return {w for w in _WORD_RE.findall((text or '').lower())
            if len(w) >= 3 and w not in _STOP}


def slug_tokens(slug: str | None) -> set[str]:
    return {w for w in (slug or '').lower().split('-')
            if len(w) >= 3 and w not in _STOP}


def match(probe: set[str], text: str | None) -> dict:
    """Word-boundary quorum match. hit iff >= min(2, len(probe)) distinct probe
    tokens appear as whole tokens in text. `matched` is sorted, for audit."""
    if not probe:
        return {'hit': False, 'matched': []}
    words = set(_WORD_RE.findall((text or '').lower()))
    matched = sorted(probe & words)
    return {'hit': len(matched) >= min(2, len(probe)), 'matched': matched}


def score_row(row: dict, assistant_text: str, correction_text: str) -> dict:
    """One injection-decision row + session text -> one v2 outcome row.
    Field names stay v1-compatible (topic_records.outcome_verdict reads
    engaged_in_assistant / topic_corrected on both versions).

    Also carries two sensitivity-parity DIAGNOSTIC fields (verdict UNCHANGED):
      engaged_matched_n  - the UNCAPPED count of matched engagement tokens
                           (engaged_matched itself stays capped at _MATCHED_CAP
                           for log/audit-file size).
      engaged_slug_only  - would the quorum rule fire using ONLY slug_tokens(slug)
                           as the probe (i.e. ignoring injected_facts)? Lets a
                           later analysis see how much engagement is actually
                           earned by facts vs. just the slug."""
    slug = row.get('injected') or (row.get('advertised') or [None])[0]
    probe = slug_tokens(slug)
    for f in (row.get('injected_facts') or []):
        probe |= tokenize(f)
    eng = match(probe, assistant_text)
    corr = match(probe, correction_text)
    slug_only = match(slug_tokens(slug), assistant_text)
    return {
        'session_id': row.get('session_id'),
        'project': row.get('project'),
        'ts': row.get('ts'),
        'tier': row.get('tier'),
        'injected': slug,
        'score': row.get('score', row.get('top_score')),
        'engaged_in_assistant': eng['hit'],
        'engaged_matched': eng['matched'][:_MATCHED_CAP],
        'engaged_matched_n': len(eng['matched']),
        'engaged_slug_only': slug_only['hit'],
        'topic_corrected': corr['hit'],
        'corrected_matched': corr['matched'][:_MATCHED_CAP],
        'scorer_version': SCORER_VERSION,
    }

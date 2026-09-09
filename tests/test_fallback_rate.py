"""Tests for scripts/fallback_rate.py — the Task 9 burn-in probe.

The pre-committed reopen threshold is fallback < 15% (rerank_timeout rows over
rows that ATTEMPTED the judge). Rows predating the flip carry no retrieval_mode
and must be excluded — they are not evidence about the rerank deploy.
"""
import fallback_rate as fr


def _row(mode=None, timeout=False, tier='high', ts='2026-07-30T10:00:00'):
    r = {'tier': tier, 'ts': ts}
    if mode is not None:
        r['retrieval_mode'] = mode
    if timeout:
        r['rerank_timeout'] = True
    return r


def test_rate_is_timeouts_over_judge_attempts():
    rows = [_row('rerank'), _row('rerank'), _row('rerank'),
            _row('lexical', timeout=True),                    # judge died -> fallback
            _row('lexical'),                                  # embed down -> never attempted
            _row(None)]                                       # pre-flip row: excluded
    s = fr.stats(rows)
    assert s['attempted'] == 4 and s['timeouts'] == 1
    assert s['rate'] == 0.25
    assert s['below_threshold'] is False                      # 25% >= 15%


def test_rate_below_threshold_passes():
    rows = [_row('rerank') for _ in range(20)] + [_row('lexical', timeout=True)]
    s = fr.stats(rows)
    assert s['timeouts'] == 1 and s['attempted'] == 21
    assert s['below_threshold'] is True                       # ~4.8% < 15%


def test_no_attempts_is_none_not_zero():
    # No post-flip rows -> no evidence; never report a fake 0% pass.
    s = fr.stats([_row(None), _row(None)])
    assert s['attempted'] == 0 and s['rate'] is None
    assert s['below_threshold'] is None


def test_since_filter_drops_older_rows():
    rows = [_row('lexical', timeout=True, ts='2026-07-29T10:00:00'),
            _row('rerank', ts='2026-07-30T10:00:00')]
    s = fr.stats(rows, since='2026-07-30')
    assert s['attempted'] == 1 and s['timeouts'] == 0


def test_error_rows_counted_separately_never_in_rate():
    rows = [_row('rerank'), {'tier': 'error', 'ts': '2026-07-30T10:00:00'}]
    s = fr.stats(rows)
    assert s['errors'] == 1 and s['attempted'] == 1


# --- prompt-length stratification (burn-in rider 1) -------------------------------
# Prefill scales with prompt tokens; paste-turns are the predicted fallback
# cluster. Concentrated-in-pastes vs uniform argue for different fixes.

def test_stats_stratifies_by_prompt_length():
    rows = [_row('rerank') | {'prompt_chars': 80},
            _row('rerank') | {'prompt_chars': 1200},
            _row('lexical', timeout=True) | {'prompt_chars': 5200},
            _row('rerank') | {'prompt_chars': 5300}]
    s = fr.stats(rows)
    by = {b['bucket']: b for b in s['by_length']}
    assert by['short']['attempted'] == 1 and by['short']['timeouts'] == 0
    assert by['medium']['attempted'] == 1
    assert by['long']['attempted'] == 2 and by['long']['timeouts'] == 1
    assert by['long']['rate'] == 0.5


def test_rows_without_prompt_chars_bucket_as_unknown():
    s = fr.stats([_row('lexical', timeout=True)])
    by = {b['bucket']: b for b in s['by_length']}
    assert by['unknown']['timeouts'] == 1


# --- --brief: the one-line form for the nightly Telegram notice -------------------

def test_brief_formats_rate_compactly():
    rows = [_row('rerank') for _ in range(19)] + [_row('lexical', timeout=True)]
    assert fr.brief(fr.stats(rows)) == '1/20 (5%)'


def test_brief_is_na_when_nothing_attempted():
    assert fr.brief(fr.stats([])) == 'n/a'


def test_brief_flags_errors_when_present():
    rows = [_row('rerank'), {'tier': 'error', 'ts': '2026-07-30T10:00:00'}]
    assert fr.brief(fr.stats(rows)) == '0/1 (0%) ⚠1err'


# --- hour stratification: schedule collisions vs prompt-length physics ------------
# Rows carry full ts; the report groups attempts by hour-of-day so a timeout
# cluster at e.g. 22:00 (nightly pipeline loading another model) reads as a
# schedule collision, not as prefill physics. Short prompt + timeout = cold
# start/eviction; long prompt + timeout = physics — hour + length together
# separate the two.

def test_stats_stratifies_attempts_by_hour():
    rows = [_row('rerank', ts='2026-07-30T09:10:00'),
            _row('rerank', ts='2026-07-30T09:40:00'),
            _row('lexical', timeout=True, ts='2026-07-30T22:05:00'),
            _row('rerank', ts='2026-07-30T22:30:00')]
    by = {b['hour']: b for b in fr.stats(rows)['by_hour']}
    assert by['09'] == {'hour': '09', 'attempted': 2, 'timeouts': 0, 'rate': 0.0}
    assert by['22']['attempted'] == 2 and by['22']['timeouts'] == 1
    assert by['22']['rate'] == 0.5


def test_hourless_rows_bucket_as_unknown_hour():
    by = {b['hour']: b for b in fr.stats([{'rerank_timeout': True, 'ts': ''}])['by_hour']}
    assert by['unknown']['timeouts'] == 1

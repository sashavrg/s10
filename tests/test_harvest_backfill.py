"""Tests for scripts/harvest_backfill.py — the bounded backlog driver.

Pure-logic coverage only: enumeration (live wins over archive, harvested
excluded, newest-first), cap bounding, and the failure accounting that keeps a
cloud outage from burning the nightly cap. harvest() itself is mocked — its
contract (idempotent, state-on-success-only, skip-on-cloud-failure) is
harvest_session's own responsibility.
"""
import json

import harvest_backfill as hb


def _tx(dirpath, sid, mtime, size=20480):
    p = dirpath / f'{sid}.jsonl'
    p.write_text('x' * size)
    import os
    os.utime(p, (mtime, mtime))
    return p


def _setup(tmp_path, monkeypatch, harvested=()):
    live = tmp_path / 'projects' / '-home-u-projA'
    arch = tmp_path / 'archive' / '-home-u-projA'
    live.mkdir(parents=True)
    arch.mkdir(parents=True)
    monkeypatch.setattr(hb, 'LIVE_ROOT', tmp_path / 'projects')
    monkeypatch.setattr(hb, 'ARCHIVE_ROOT', tmp_path / 'archive')
    state = tmp_path / 'harvested_sessions.json'
    state.write_text(json.dumps({'sessions': {s: {} for s in harvested}}))
    monkeypatch.setattr(hb, 'HARVEST_STATE_PATH', state)
    return live, arch


def test_backlog_excludes_harvested_and_prefers_live_copy(tmp_path, monkeypatch):
    live, arch = _setup(tmp_path, monkeypatch, harvested=['s-old'])
    _tx(arch, 's-old', 1000)              # harvested -> excluded
    _tx(arch, 's-both', 2000)             # shadowed by the live copy
    live_both = _tx(live, 's-both', 3000)
    _tx(arch, 's-arch', 1500)
    items = hb.backlog()
    ids = [p.stem for p in items]
    assert 's-old' not in ids
    assert ids.count('s-both') == 1
    assert next(p for p in items if p.stem == 's-both') == live_both


def test_backlog_is_newest_first(tmp_path, monkeypatch):
    live, _ = _setup(tmp_path, monkeypatch)
    _tx(live, 's-jan', 1000)
    _tx(live, 's-mar', 3000)
    _tx(live, 's-feb', 2000)
    assert [p.stem for p in hb.backlog()] == ['s-mar', 's-feb', 's-jan']


def test_run_respects_cap_and_reports(tmp_path, monkeypatch):
    live, _ = _setup(tmp_path, monkeypatch)
    for i in range(5):
        _tx(live, f's{i}', 1000 + i)
    calls = []

    def fake_harvest(path, project, spec, force=False):
        calls.append((path.stem, project))
        return {'status': 'ok', 'corrections_written': 1, 'duplicates_dropped': 0}
    monkeypatch.setattr(hb, 'harvest', fake_harvest)
    summary = hb.run(cap=3, spec='claude:sonnet')
    assert len(calls) == 3
    assert summary == {'attempted': 3, 'ok': 3, 'skipped': 0, 'errors': 0,
                       'corrections_written': 3, 'remaining': 2}
    assert calls[0][1] == '-home-u-projA'   # project = transcript dir name


def test_run_stops_early_on_consecutive_cloud_skips(tmp_path, monkeypatch):
    # A cloud outage must not burn the whole cap on doomed calls: after
    # STOP_AFTER_SKIPS consecutive skips the run stops and leaves the rest
    # for the next night (harvest records no state on skip, so nothing is lost).
    live, _ = _setup(tmp_path, monkeypatch)
    for i in range(10):
        _tx(live, f's{i}', 1000 + i)
    monkeypatch.setattr(hb, 'harvest', lambda *a, **k: {
        'status': 'skipped', 'reason': 'cloud LLM unavailable: quota'})
    summary = hb.run(cap=10, spec='claude:sonnet')
    assert summary['attempted'] == hb.STOP_AFTER_SKIPS
    assert summary['ok'] == 0 and summary['skipped'] == hb.STOP_AFTER_SKIPS
    assert summary['remaining'] == 10 - hb.STOP_AFTER_SKIPS


def test_run_dry_run_calls_nothing(tmp_path, monkeypatch):
    live, _ = _setup(tmp_path, monkeypatch)
    _tx(live, 's0', 1000)
    monkeypatch.setattr(hb, 'harvest', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('harvest must not be called in dry-run')))
    summary = hb.run(cap=5, spec='claude:sonnet', dry_run=True)
    assert summary == {'attempted': 0, 'ok': 0, 'skipped': 0, 'errors': 0,
                       'corrections_written': 0, 'remaining': 1}


def test_backlog_excludes_machine_session_dirs(tmp_path, monkeypatch):
    """-tmp holds the KB's OWN headless `claude -p` transcripts (kb runs them
    from tempfile.gettempdir(), and KB_HEADLESS means no hooks, no human turns).
    Harvesting them is the system reading its own exhaust — and each harvest
    call CREATES one, so unexcluded they make the queue self-feeding (observed
    2026-08-20: 60/60 calls burned on -tmp, 0 corrections, 92% of the backlog)."""
    live, _ = _setup(tmp_path, monkeypatch)
    tmpdir = live.parent / '-tmp'
    tmpdir.mkdir()
    _tx(tmpdir, 's-machine', 9000)          # newest — would top the queue
    _tx(live, 's-human', 1000)
    assert [p.stem for p in hb.backlog()] == ['s-human']

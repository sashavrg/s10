#!/usr/bin/env python3
"""Terminal labeler for the ej9 blind gate dossier (A4 of
docs/superpowers/specs/2026-07-20-ej9-gate-read-addendum.md).

Reading 35 rows of dense evidence as one markdown wall does not work, and the
durable label-protocol lesson (2026-07-03) is that every label error so far came
from *context-poor* labeling — so the fix is presentation, never less evidence:
one row per screen with the core visible, and the long tail (full trigger, full
reply, later mentions, the whole transcript, the topic page) one key away.

  python scripts/label_gate.py            # resume at the first unlabeled row
  python scripts/label_gate.py --row 12   # jump to a row
  python scripts/label_gate.py --status   # progress, then exit

Answers are written to the dossier as they are given, so quitting mid-session
loses nothing and `gate_dossier.py parse` reads the same file back. The dossier
is DATA (evals/candidates/ is gitignored) — never commit it.

This tool NEVER proposes a label: it renders evidence and records the operator's
answer. The gate measures a frozen Claude judge against operator labels; a
machine-suggested label would collapse that into Claude-vs-Claude agreement.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from injection_outcome import parse_turns

BASE_DIR = Path(__file__).resolve().parent.parent
DOSSIER_PATH = BASE_DIR / 'evals' / 'candidates' / 'ej9-gate-dossier.md'

FIELDS = ('identity_matches_subject', 'label_engaged', 'label_corrected')
QUESTIONS = {
    'identity_matches_subject':
        'Do the shown facts coherently represent THIS slug\'s actual subject?',
    'label_engaged':
        'Did the assistant genuinely work THIS topic\'s subject at that moment?',
    'label_corrected':
        'Was the topic\'s CONTENT wrong/contested by you that session?',
}

_ROW_RE = re.compile(r'^## Row (\d+) — `([^`]+)`')
_SESSION_RE = re.compile(r'^_session `([^`]+)` · ts (\S+) · transcript: (.+)_$')
_TOPIC_RE = re.compile(r'^_full topic page: (.+)_$')

BOLD, DIM, RESET = '\033[1m', '\033[2m', '\033[0m'


# --------------------------------------------------------------------- parse

def parse_rows(md: str) -> list[dict]:
    """Every row of the rendered dossier, with its evidence sections split out.

    Pure. Mirrors `gate_dossier.render_dossier`; unknown lines are ignored so a
    dossier the operator has annotated by hand still parses."""
    rows: list[dict] = []
    cur: dict | None = None
    section: str | None = None
    for i, raw in enumerate(md.splitlines()):
        line = raw.strip()
        m = _ROW_RE.match(line)
        if m:
            cur = {'ordinal': int(m.group(1)), 'slug': m.group(2),
                   'session_id': '', 'ts': '', 'transcript': '', 'topic_page': '',
                   'facts': [], 'trigger': '', 'reply': '', 'mentions': [],
                   'labels': {f: None for f in FIELDS}, 'start': i}
            rows.append(cur)
            section = None
            continue
        if cur is None:
            continue
        if m := _SESSION_RE.match(line):
            cur['session_id'], cur['ts'], cur['transcript'] = m.groups()
        elif m := _TOPIC_RE.match(line):
            cur['topic_page'] = m.group(1)
        elif line.startswith('**Injected facts'):
            section = 'facts'
        elif line.startswith('**Trigger prompt:**'):
            cur['trigger'] = line[len('**Trigger prompt:**'):].strip()
            section = None
        elif line.startswith('**Assistant reply:**'):
            cur['reply'] = line[len('**Assistant reply:**'):].strip()
            section = None
        elif line.startswith('**Later mentions:**'):
            section = 'mentions'
        elif section == 'facts' and line.startswith('- '):
            cur['facts'].append(line[2:].strip())
        elif section == 'mentions' and line.startswith('· '):
            cur['mentions'].append(line[2:].strip())
        else:
            for f in FIELDS:
                if line.startswith(f'- {f}:'):
                    v = line.split(':', 1)[1].strip().lower()
                    cur['labels'][f] = {'yes': True, 'no': False}.get(v)
                    section = None
    return rows


def pending(rows: list[dict]) -> list[dict]:
    """Rows still missing at least one of the three answers."""
    return [r for r in rows if any(r['labels'][f] is None for f in FIELDS)]


def set_labels(md: str, ordinal: int, values: dict) -> str:
    """Write ``values`` into row ``ordinal``'s field lines. Pure string→string.

    Only those lines change — the dossier is the session's state and the gate
    read parses this same file. Partial writes are allowed (answering one field
    at a time survives a quit); unknown ordinals and unrecognized values raise.

    Accepts the TUI's single keystrokes (y/n) as well as yes/no, but always
    WRITES the canonical yes/no: `gate_dossier.parse_dossier` recognizes nothing
    else, so a bare 'y' in the file would read back as an unlabeled row."""
    canon = {'y': 'yes', 'yes': 'yes', 'n': 'no', 'no': 'no'}
    bad = {k: v for k, v in values.items()
           if k not in FIELDS or str(v).strip().lower() not in canon}
    if bad:
        raise ValueError(f'labels must be y/n or yes/no on {FIELDS}, got {bad}')
    values = {k: canon[str(v).strip().lower()] for k, v in values.items()}
    lines = md.splitlines()
    starts = [(int(m.group(1)), i) for i, ln in enumerate(lines)
              if (m := _ROW_RE.match(ln.strip()))]
    hit = [k for k, (o, _) in enumerate(starts) if o == ordinal]
    if not hit:
        raise KeyError(f'no Row {ordinal} in this dossier')
    k = hit[0]
    end = starts[k + 1][1] if k + 1 < len(starts) else len(lines)
    written = set()
    for i in range(starts[k][1], end):
        for f, v in values.items():
            if lines[i].strip().startswith(f'- {f}:'):
                lines[i] = f'- {f}: {str(v).lower()}'
                written.add(f)
    if written != set(values):
        raise KeyError(f'Row {ordinal}: missing field lines {set(values) - written}')
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------- display

def wrap_block(text: str, width: int, indent: str = '  ') -> list[str]:
    """Wrapped, indented lines for ``text`` ([] when there is nothing to show)."""
    if not text.strip():
        return []
    return textwrap.wrap(text, width=max(20, width - len(indent)),
                         initial_indent=indent, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


def head_lines(lines: list[str], limit: int) -> tuple[list[str], int]:
    """First ``limit`` lines + how many were withheld (0 when nothing was cut)."""
    return lines[:limit], max(0, len(lines) - limit)


def _page(title: str, body: str) -> None:
    """Show long evidence in the pager (falls back to plain print)."""
    text = f'{title}\n{"─" * len(title)}\n\n{body}\n'
    try:
        subprocess.run(['less', '-R'], input=text, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        print(text)
        input('  [enter] ')


def _transcript_body(path: str) -> str:
    turns = parse_turns(Path(path))
    if not turns:
        return f'(no turns parsed from {path})'
    out = []
    for t in turns:
        who = 'USER' if t.get('role') == 'user' else 'ASSISTANT'
        out += [f'▸ {who}', textwrap.indent(t.get('text') or '', '    '), '']
    return '\n'.join(out)


def render(row: dict, n_rows: int, n_done: int, width: int) -> str:
    rule = '─' * width
    head = (f"{BOLD}Row {row['ordinal']}/{n_rows} — {row['slug']}{RESET}"
            f"{DIM}   [labeled {n_done} · open {n_rows - n_done}]{RESET}")
    out = [head, f"{DIM}  session {row['session_id']} · ts {row['ts']}{RESET}", rule, '']

    out.append(f'{BOLD}INJECTED FACTS{RESET} (the identity you are rating)')
    for f in row['facts']:
        out += textwrap.wrap(f, width=max(20, width - 4), initial_indent='  • ',
                             subsequent_indent='    ', break_long_words=False,
                             break_on_hyphens=False)
    if not row['facts']:
        out.append('  (none recorded)')
    out.append('')

    trig = wrap_block(row['trigger'], width)
    shown, cut = head_lines(trig, 6)
    out.append(f"{BOLD}TRIGGER PROMPT{RESET}"
               + (f"{DIM}   [t] +{cut} more lines{RESET}" if cut else ''))
    out += shown + ['']

    reply = wrap_block(row['reply'], width)
    shown, cut = head_lines(reply, 8)
    out.append(f"{BOLD}ASSISTANT REPLY{RESET}"
               + (f"{DIM}   [r] +{cut} more lines{RESET}" if cut else ''))
    out += shown + ['']

    keys = []
    if row['mentions']:
        keys.append(f"[m] {len(row['mentions'])} later mentions")
    keys += ['[x] open transcript', '[p] topic page', '[?] help']
    out += [f'{DIM}  ' + '   '.join(keys) + RESET, rule]
    return '\n'.join(out)


HELP = """  y / n     answer the current question
  t / r / m full trigger prompt · full assistant reply · later mentions
  x / p     open the whole transcript · the full topic page (pager)
  b         back one question (re-answer)
  s         skip this row for now (comes back at the end)
  q         save and quit — answers already given are on disk
"""


# ---------------------------------------------------------------------- loop

def _ask(row: dict, field: str, n_rows: int, n_done: int) -> str:
    """Prompt until the operator answers or navigates away. Returns y/n/b/s/q."""
    while True:
        width = min(100, shutil.get_terminal_size((100, 30)).columns) - 2
        print('\033[2J\033[H' + render(row, n_rows, n_done, width))
        idx = FIELDS.index(field) + 1
        print(f'  {BOLD}{idx}/3 {QUESTIONS[field]}{RESET}')
        try:
            ans = input(f'  {field}? [y/n] ▸ ').strip().lower()
        except EOFError:
            return 'q'
        if ans in ('y', 'yes'):
            return 'y'
        if ans in ('n', 'no'):
            return 'n'
        if ans in ('b', 's', 'q'):
            return ans
        if ans == 't':
            _page(f"TRIGGER PROMPT — row {row['ordinal']} ({row['slug']})", row['trigger'])
        elif ans == 'r':
            _page(f"ASSISTANT REPLY — row {row['ordinal']} ({row['slug']})", row['reply'])
        elif ans == 'm':
            _page(f"LATER MENTIONS — row {row['ordinal']} ({row['slug']})",
                  '\n\n'.join(f'· {s}' for s in row['mentions']) or '(none)')
        elif ans == 'x':
            _page(f"TRANSCRIPT — {row['transcript']}", _transcript_body(row['transcript']))
        elif ans == 'p':
            page = BASE_DIR / row['topic_page']
            body = page.read_text(errors='replace') if page.exists() else f'(missing: {page})'
            _page(f"TOPIC PAGE — {row['topic_page']}", body)
        elif ans in ('?', 'h', 'help'):
            _page('KEYS', HELP)


def label_session(path: Path, start_ordinal: int | None, revisit_all: bool) -> int:
    rows = parse_rows(path.read_text())
    n_rows = len(rows)
    queue = rows if revisit_all else pending(rows)
    if start_ordinal is not None:
        queue = [r for r in rows if r['ordinal'] >= start_ordinal] if revisit_all else \
                [r for r in queue if r['ordinal'] >= start_ordinal]
    if not queue:
        print(f'all {n_rows} rows labeled — next: python scripts/gate_dossier.py parse')
        return 0

    skipped: list[dict] = []
    i = 0
    while i < len(queue):
        row = queue[i]
        n_done = n_rows - len(pending(parse_rows(path.read_text())))
        # Fields already on disk when this row opened are auto-skipped (resume).
        # Snapshot them, so answering a field NOW doesn't make it unrewindable:
        # skipping on the live labels would swallow the back key.
        prefilled = set() if revisit_all else {
            f for f in FIELDS if row['labels'][f] is not None}
        field_i = 0
        while field_i < len(FIELDS):
            field = FIELDS[field_i]
            if field in prefilled:
                field_i += 1
                continue
            ans = _ask(row, field, n_rows, n_done)
            if ans == 'q':
                done = n_rows - len(pending(parse_rows(path.read_text())))
                print(f'\nsaved — {done}/{n_rows} rows labeled. Re-run to resume.')
                return 0
            if ans == 's':
                skipped.append(row)
                break
            if ans == 'b':
                earlier = [j for j in range(field_i) if FIELDS[j] not in prefilled]
                field_i = earlier[-1] if earlier else field_i   # nothing to rewind to
                continue
            path.write_text(set_labels(path.read_text(), row['ordinal'], {field: ans}))
            row['labels'][field] = (ans == 'y')
            field_i += 1
        i += 1
        if i == len(queue) and skipped:      # skipped rows come back at the end
            queue, skipped, i = skipped, [], 0

    done = n_rows - len(pending(parse_rows(path.read_text())))
    print(f'\n{done}/{n_rows} rows labeled.')
    if done == n_rows:
        print('next: python scripts/gate_dossier.py parse')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='Blind gate dossier labeler (A4).')
    ap.add_argument('--dossier', type=Path, default=DOSSIER_PATH)
    ap.add_argument('--row', type=int, help='start at this ordinal')
    ap.add_argument('--all', action='store_true',
                    help='walk every row, including already-answered ones')
    ap.add_argument('--status', action='store_true', help='print progress and exit')
    args = ap.parse_args()

    if not args.dossier.exists():
        print(f'no dossier at {args.dossier} — run: python scripts/gate_dossier.py make')
        return 1
    rows = parse_rows(args.dossier.read_text())
    if args.status:
        open_rows = [r['ordinal'] for r in pending(rows)]
        print(f'labeled {len(rows) - len(open_rows)}/{len(rows)}')
        print('open: ' + (', '.join(map(str, open_rows)) or 'none'))
        return 0
    return label_session(args.dossier, args.row, args.all)


if __name__ == '__main__':
    raise SystemExit(main())

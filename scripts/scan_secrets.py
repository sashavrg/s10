#!/usr/bin/env python3
"""Commit-time secret gate for llm-kb.

The data/machinery split (see CLAUDE.md "Data vs machinery") keeps personal KB
*data* out of git via .gitignore. But the leak that prompted this lived in
*machinery* — a DB connection string pasted into a tracked doc. .gitignore can't
help there: docs are supposed to be tracked. This scanner closes that gap.

Design priorities, in order:
  1. Catch the leak that actually happened — a URI with inline credentials.
  2. Catch the common high-confidence token formats (AWS / GitHub / private keys).
  3. DO NOT false-positive on this repo's real content. A scanner that cries wolf
     gets bypassed with `--no-verify`, which is worse than no scanner. So the
     `keyword = value` rule skips placeholders (`<REDACTED>`, `${ENV}`, `your_token`,
     `example`, …) and only the keyword *immediately* adjacent to `=`/`:` counts
     (so `tokenizer = ...` is not a "token = ...").

Stdlib only (matches the rest of scripts/). Output is self-redacting: the raw
secret is never printed — the hook echoes this to the terminal.

Usage:
  scan_secrets.py [PATH ...]   # scan given files (default: all git-tracked files)
  scan_secrets.py --staged     # scan staged blob content (the pre-commit hook)
Exit status: 0 = clean, 1 = secret(s) found, 2 = usage/git error.

Escape hatch: append `# pragma: allowlist secret` (or `gitleaks:allow`) to a line
to suppress it — use only for genuine non-secrets (test fixtures, sample keys).
"""
import re
import subprocess
import sys
from dataclasses import dataclass

ALLOWLIST_MARKERS = ('pragma: allowlist secret', 'gitleaks:allow')

# Values that look like secrets but are not. The placeholder filter is what keeps
# this scanner usable inside a docs-heavy repo.
_PLACEHOLDER_SUBSTRINGS = (
    'redacted', 'example', 'placeholder', 'changeme', 'change-me', 'change_me',
    'your_', 'your-', 'yourtoken', 'dummy', 'sample', 'xxxx', 'todo', 'fixme',
    'notarealsecret', 'not-a-real', 'insertyour', 'replace_me', 'replace-me',
)
_ENV_REF_RE = re.compile(r'^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$')   # $VAR or ${VAR}
_BARE_ENVNAME_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,}$')          # PORTAL_API_SECRET
# kebab/slash identifier, lowercase, no digits — a slug or project name, not a
# secret (real secrets carry digits / underscores / mixed case). Used to spare
# config/source_projects.yaml, whose slugs sometimes end in token/key/secret.
_IDENT_RE = re.compile(r'^[a-z][a-z]*(?:[-/][a-z]+)*$')


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip('\'"')
    if len(v) < 8:
        return True
    low = v.lower()
    if any(s in low for s in _PLACEHOLDER_SUBSTRINGS):
        return True
    if '<' in v or '>' in v:                       # <REDACTED>, <your-key>
        return True
    if _ENV_REF_RE.match(v):                       # $TOKEN / ${TOKEN}
        return True
    if _BARE_ENVNAME_RE.match(v):                  # assigning one var name to another
        return True
    if len(set(v)) <= 2:                           # ******** , xxxxxxxx
        return True
    return False


def _looks_like_plain_identifier(value: str) -> bool:
    """A lowercase kebab/slash identifier with no digits — a slug or name, not a
    secret. Applied only to the assignment rule (a credential inside a URI stays
    suspicious regardless of how plain it looks)."""
    return bool(_IDENT_RE.match(value.strip().strip('\'"')))


# --- rules ----------------------------------------------------------------
# Filtered rules capture a value and skip it when it looks like a placeholder.
# Format rules are specific enough to flag unconditionally.

# scheme://user:PASSWORD@host  — the exact shape of the real leak.
_CONN_RE = re.compile(r'(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s:/@]+)@')

# keyword <:=> value, keyword immediately adjacent to the separator.
_ASSIGN_RE = re.compile(
    r'(?i)(?:password|passwd|pwd|secret|secret[_-]?key|token|api[_-]?key|'
    r'access[_-]?key|client[_-]?secret|auth[_-]?token|bot[_-]?token|'
    r'private[_-]?key)\s*[:=]\s*["\']?([^\s"\'#]{8,})'
)

# username:password with no scheme, no @host, no keyword — the exact shape of the
# leak that prompted this gate (a bare `User:password` pasted into a doc).
# LHS is a plain username (letters/digits/underscore, NO dots or hyphens, so model
# tags like `qwen2.5-coder:…` don't match); RHS is validated by _is_credential_value
# so `host:port`, `sha256:<hex>`, and `12:30` are excluded.
# The lookbehind forces the username to start at a real boundary (start, space,
# quote, paren, backtick) — so `qwen2.5-coder:7b…` is not read as `coder:7b…`.
# RHS must follow the colon with NO whitespace, which also spares YAML `slug: name`.
_CRED_PAIR_RE = re.compile(
    r'(?<![A-Za-z0-9._/\-])([A-Za-z][A-Za-z0-9_]{2,40}):([A-Za-z0-9][A-Za-z0-9._\-]{9,80})'
)
_HEX_RE = re.compile(r'[0-9a-fA-F]{16,}$')
_VERSIONISH_RE = re.compile(r'\d[\d.]+[A-Za-z]*\d*$')


def _is_credential_value(v: str) -> bool:
    """True if `v` (the RHS of a `user:v` pair) looks like a real password rather
    than a port, hash, version, slug, or placeholder."""
    if len(v) < 12 or _is_placeholder(v) or _looks_like_plain_identifier(v):
        return False
    if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
        return False                       # need letters AND digits (entropy proxy)
    if _HEX_RE.fullmatch(v):               # git SHAs / content digests
        return False
    if _VERSIONISH_RE.fullmatch(v):        # 1.2.3, 7b-instruct... handled elsewhere
        return False
    if re.search(r'\d+\.\d+', v):          # dotted version → model tag / semver
        return False
    return True

# (rule, regex, skip_plain_identifiers)
_FILTERED_RULES = (
    ('connection-string-credentials', _CONN_RE, False),
    ('hardcoded-secret-assignment', _ASSIGN_RE, True),
)

_FORMAT_RULES = (
    ('aws-access-key-id', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('github-token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}\b')),
    ('github-pat', re.compile(r'\bgithub_pat_[A-Za-z0-9_]{60,}\b')),
    ('slack-token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}')),
    ('google-api-key', re.compile(r'\bAIza[0-9A-Za-z_\-]{35}\b')),
    ('openai-key', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b')),
    ('anthropic-key', re.compile(r'\bsk-ant-[A-Za-z0-9_\-]{24,}')),
    ('private-key-block', re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')),
)


@dataclass
class Finding:
    lineno: int
    rule: str
    preview: str        # self-redacting; safe to print


def _mask(secret: str) -> str:
    s = secret.strip().strip('\'"')
    if len(s) <= 6:
        return '*' * len(s)
    return f'{s[:2]}{"*" * (len(s) - 5)}{s[-3:]}'


def find_secrets(text: str) -> list:
    """Return Findings for `text` (may be multi-line). Self-redacting previews."""
    findings = []
    for i, line in enumerate(text.splitlines(), start=1):
        if any(m in line for m in ALLOWLIST_MARKERS):
            continue
        for rule, rx, skip_ident in _FILTERED_RULES:
            for m in rx.finditer(line):
                value = m.group(1)
                if _is_placeholder(value):
                    continue
                if skip_ident and _looks_like_plain_identifier(value):
                    continue
                findings.append(Finding(i, rule, f'{rule}: …{_mask(value)}…'))
        for rule, rx in _FORMAT_RULES:
            for m in rx.finditer(line):
                if 'EXAMPLE' in m.group(0).upper():     # AWS's documented sample key
                    continue
                findings.append(Finding(i, rule, f'{rule}: …{_mask(m.group(0))}…'))
        for m in _CRED_PAIR_RE.finditer(line):
            value = m.group(2)
            if _is_credential_value(value):
                findings.append(Finding(i, 'credential-pair', f'credential-pair: …{_mask(value)}…'))
    return findings


# --- git plumbing ---------------------------------------------------------

def _git(*args: str) -> str:
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout


def _staged_files() -> list:
    out = _git('diff', '--cached', '--name-only', '--diff-filter=ACM', '-z')
    return [p for p in out.split('\0') if p]


def _looks_binary(blob: str) -> bool:
    return '\0' in blob


def scan_paths(paths, staged: bool) -> int:
    """Scan paths (or staged blobs). Print redacted findings; return exit code."""
    total = 0
    for path in paths:
        try:
            if staged:
                blob = _git('show', f':{path}')
            else:
                with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                    blob = fh.read()
        except (subprocess.CalledProcessError, OSError):
            continue
        if _looks_binary(blob):
            continue
        for f in find_secrets(blob):
            print(f'  {path}:{f.lineno}: {f.preview}')
            total += 1
    if total:
        where = 'staged changes' if staged else 'tracked files'
        print(f'\nscan_secrets: {total} possible secret(s) in {where}.')
        print('If a match is a genuine non-secret, append `# pragma: allowlist secret`')
        print('to that line. To bypass the check entirely: git commit --no-verify')
        return 1
    return 0


def main(argv) -> int:
    staged = '--staged' in argv
    rest = [a for a in argv if not a.startswith('-')]
    try:
        if staged:
            paths = _staged_files()
        elif rest:
            paths = rest
        else:
            paths = [p for p in _git('ls-files', '-z').split('\0') if p]
    except subprocess.CalledProcessError as e:
        print(f'scan_secrets: git error: {e}', file=sys.stderr)
        return 2
    return scan_paths(paths, staged)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))

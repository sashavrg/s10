"""scripts/scan_secrets.py is the commit-time secret gate (the second half of the
data/machinery hardening — gitignore keeps *data* out of git; this keeps *secrets*
out of the tracked *machinery*, which is where the real leak happened: a DB
connection string pasted into a tracked doc).

It must catch that class of leak WITHOUT false-positiving on the placeholders that
legitimately live in this repo's docs and config templates: `<REDACTED>`,
`${ENV_VARS}`, `your_token`, `example`. A noisy scanner gets disabled, which is
worse than none — so the negative cases below are as important as the positives.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import scan_secrets as ss  # noqa: E402


# --- positives: these MUST be flagged -------------------------------------

def test_flags_mongodb_uri_with_inline_credentials():
    # the exact shape of the real leak (synthetic value — never commit the real one)
    text = 'conn = "mongodb://SampleDbUser:Xy2z-pretend-passw0rd_0042@localhost:27017/?tls=false"'  # pragma: allowlist secret
    hits = ss.find_secrets(text)
    assert hits, "connection string with inline creds must be flagged"
    assert hits[0].lineno == 1


def test_flags_password_assignment_with_real_value():
    assert ss.find_secrets("db_password = 'hunter2-correcthorsebattery'")  # pragma: allowlist secret


def test_flags_aws_access_key():
    assert ss.find_secrets("aws_key=AKIAZ1Y2X3W4V5U6T7S8")  # pragma: allowlist secret


def test_flags_github_token():
    assert ss.find_secrets("token: ghp_" + "a" * 36)


def test_flags_private_key_header():
    assert ss.find_secrets("-----BEGIN RSA PRIVATE KEY-----")  # pragma: allowlist secret


def test_flags_bare_username_password_pair():
    # THE leak that prompted this: a `user:password` pair in prose, no scheme,
    # no @host, no `password:` keyword. Must still be caught.
    assert ss.find_secrets("creds (`SampleDbUser:Xy2z-pretend-passw0rd_0042`) in the doc")  # pragma: allowlist secret


def test_credential_pair_rule_does_not_flag_repo_idioms():
    # the things in this repo that look like `word:word` but are not secrets
    assert ss.find_secrets("TOPIC_MODEL = qwen2.5-coder:7b-instruct-q4_K_M") == []
    assert ss.find_secrets("model: qwen2.5:7b-instruct-q4_K_M") == []
    assert ss.find_secrets("digest sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4") == []
    assert ss.find_secrets("server at localhost:11434 and 192.0.2.5:8080") == []
    assert ss.find_secrets("meeting at 12:30, ratio 16:9") == []
    # backend:model specs are everywhere in this repo; the dotted version is the tell
    assert ss.find_secrets("default ollama:qwen2.5-coder:7b spec") == []
    assert ss.find_secrets("route via claude:sonnet then fallback") == []


def test_redacts_the_value_in_its_report():
    secret = "Xy2z-pretend-passw0rd_0042"  # pragma: allowlist secret
    text = f'mongodb://SampleDbUser:{secret}@localhost:27017'  # pragma: allowlist secret
    hits = ss.find_secrets(text)
    assert hits
    # the report must never echo the raw secret (the hook prints it to the terminal)
    assert secret not in hits[0].preview


# --- negatives: real repo content that must NOT be flagged ----------------

def test_ignores_redacted_placeholder():
    assert ss.find_secrets("mongodb://SampleDbUser:<REDACTED>@localhost:27017") == []


def test_ignores_env_var_reference():
    assert ss.find_secrets("secret: ${PORTAL_API_SECRET}") == []
    assert ss.find_secrets("password = $DB_PASSWORD") == []


def test_ignores_example_and_named_placeholders():
    assert ss.find_secrets("TELEGRAM_BOT_TOKEN=your_token") == []
    assert ss.find_secrets("password: changeme") == []
    assert ss.find_secrets('uri = "mongodb://user:example@example.com"') == []


def test_does_not_flag_tokenizer_like_identifiers():
    # 'token' appears in code constantly; only `keyword <:=> value` counts
    assert ss.find_secrets("tokenizer = AutoTokenizer.from_pretrained('qwen')") == []
    assert ss.find_secrets("access_count: 12") == []


def test_ignores_slug_to_project_name_mapping():
    # config/source_projects.yaml maps slugs (some ending in token/key/secret) to
    # plain project names. A kebab-case identifier value is not a secret; a real
    # secret has digits / underscores / mixed case (so this skip can't hide one).
    assert ss.find_secrets("icon-circle-token: acme-portal-api") == []
    assert ss.find_secrets("api-secret-key: stable-diffusion-webui-docker") == []
    # ...but a digit/underscore-bearing value with the same keyword IS flagged
    assert ss.find_secrets("db-token: Xy2z-pretend-passw0rd_0042")  # pragma: allowlist secret


def test_allowlist_pragma_suppresses_line():
    text = 'token = ghp_' + 'a' * 36 + '  # pragma: allowlist secret'
    assert ss.find_secrets(text) == []


def test_scan_text_reports_line_numbers():
    text = "clean line\nmongodb://u:realpass-correcthorse@h\nanother clean line\n"  # pragma: allowlist secret
    hits = ss.find_secrets(text)
    assert len(hits) == 1
    assert hits[0].lineno == 2

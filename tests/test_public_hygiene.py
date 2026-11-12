"""Guards for the public release: no personal data, credentials or private references anywhere in the tree,
and a README that carries the first-run essentials.

Every tracked text file is scanned (`git ls-files`; a plain folder walk where there is no git). Each class of
leak has its own check and a self-test that proves it fires. Names of private people, projects and places are
never written here: they live in a private deny-list outside the repository (PRIVATE_TERMS), read only when
present, so the public copy of this file reveals nothing and still runs every generic check.
"""
import os
import pwd
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()
# Kept in the private repository only and left out of the public export (docs/RELEASE_PLAN.md, step 6).
PRIVATE_ONLY = {"docs/RELEASE_PLAN.md", "uv.lock"}
PRIVATE_PREFIXES = (".codex/",)
SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache", ".ref"}
# One case-insensitive regular expression per line; '#' starts a comment. Outside the repository on purpose.
PRIVATE_TERMS = Path(os.environ.get("HARNESSGRAM_PRIVATE_TERMS")
                     or Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "harnessgram" / "private-terms.txt")


def _tracked():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30).stdout
        names = [n for n in out.decode("utf-8").split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        names = []
    if not names:
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
            names += [(Path(base) / f).relative_to(ROOT).as_posix() for f in files]
    return sorted(n for n in names if n not in PRIVATE_ONLY and not n.startswith(PRIVATE_PREFIXES))


def _text_files():
    out = []
    for name in _tracked():
        try:
            data = (ROOT / name).read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue  # binary (pictures and the like)
        try:
            out.append((name, data.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return out


FILES = _text_files()


def _lines(include_self: bool = False):
    assert FILES
    for name, text in FILES:
        if name == SELF and not include_self:
            continue  # this file holds the patterns themselves (built from pieces where they would match)
        for number, line in enumerate(text.splitlines(), 1):
            yield f"{name}:{number}", line


# --- what counts as an obvious placeholder ------------------------------------------------------------------

def _fake_secret(body: str) -> bool:
    """A made-up credential body: few distinct characters, a run like 'abcdef' or '012345', or a telling word."""
    low = body.lower()
    return len(set(low)) < 8 or any(w in low for w in ("abcdef", "012345", "secret", "example", "fake", "dummy",
                                                       "test", "xxxx", "redact"))


def _fake_phone(number: str) -> bool:
    digits = re.sub(r"\D", "", number)
    return (len(set(digits)) <= 3 or any(run in digits for run in ("1234567", "0000000", "9876543"))
            or re.search(r"(?:^1|^)555\d{4}$|^1?\d{3}55501\d\d$", digits) is not None)


# --- the checks: each returns the offending matches in one line ----------------------------------------------

PATTERNS = {
    "personal paths": re.compile(r"/Users" r"/(?!u/|me/|you/|someone/|x/|example/)[A-Za-z]|[A-Za-z]:\\Users\\"),
    # A note that names the owner and a date on one line, in either order ("(owner, 2026-...)", "2026-...: the owner
    # asked", "owner request 2026-..."), or points at the owner's private rules file: private history, not a
    # technical explanation.
    "dated owner notes": re.compile(r"\bowner\b[^\n]{0,100}\b202\d-\d\d(?:-\d\d)?\b|\b202\d-\d\d(?:-\d\d)?\b[^\n]{0,100}\bowner\b"
                                    r"|\(owner,? ?20\d\d|OWNER_WORK_RULES", re.I),
    "bot tokens": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"),
}
CREDENTIAL = re.compile(
    r"(?:AIza|sk-ant-|sk-(?:proj-)?|gh[pousr]_|github_pat_|xox[abprs]-|AKIA|ASIA|glpat-|ya29\.|pypi-|"
    r"hf_|SG\.|sq0atp-|rk_live_|sk_live_|pk_live_)([A-Za-z0-9_.-]{16,})|\bnpm_([A-Za-z0-9]{36})\b")
PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
PEM_BODY = re.compile(r"^\s*[\"']?[A-Za-z0-9+/]{40,}={0,2}")
PHONE = re.compile(r"(?<![\w:.-])(?:\+\d[\d ()-]{7,}\d|0\d{1,2}-?\d{7}|\(\d{3}\) ?\d{3}-\d{4})(?![\w-])")
# Cloud project ids: in an API path, a config key, a CLI flag or a Supabase host.
PROJECT = re.compile(r"projects/([a-z][a-z0-9-]{3,}[a-z0-9])/|(?:gemini_vertex_project|project_id|GOOGLE_CLOUD_PROJECT)"
                     r"[\"']?\s*[:=]\s*[\"']([^\"'\s]{4,})|--project[ =]([a-z][\w-]{3,})|([a-z0-9]{20})\.supabase\.co")
PLACEHOLDER_PROJECTS = {"my-project", "example-project", "your-project", "my-gcp-project"}
URL = re.compile(r"\b(?:https?|wss?|rtmps?)://([A-Za-z0-9.-]+)")
# Every host named in the tree. A new one is added here on purpose, so a private address cannot slip in.
PUBLIC_HOSTS = {
    "t.me", "github.com", "raw.githubusercontent.com", "claude.com", "claude.ai", "code.claude.com", "api.anthropic.com", "api.telegram.org",
    "telegram.org", "web.telegram.org", "my.telegram.org", "aiplatform.googleapis.com",
    "generativelanguage.googleapis.com", "translate.googleapis.com", "clients5.google.com", "pypi.org",
    "www.w3.org", "supabase.com", "docs.astral.sh", "pipx.pypa.io", "www.googleapis.com", "accounts.google.com",
    "oauth2.googleapis.com",  # YouTube API
    "localhost", "127.0.0.1", "x", "a", "hgl", "user", "claude", "x.y", "...", "host",
}
HOST_OK = re.compile(r"^example\.|(?:^|\.)t\.me$|(?:^|\.)(?:example(?:\.(?:com|org|net|test|invalid))?|test|invalid|localhost)$"
                     r"|^[\w-]+\.tailabc\w*\.ts\.net$|^claude\.com\.evil\.io$", re.I)
KEYCHAIN = re.compile(r"find-generic-password[^\n]*?[\"']-s[\"'],\s*[\"']([^\"']+)|security\s+find-generic-password"
                      r"\s+(?:-a\s+\S+\s+)?-s\s+([\w.@-]+)")
PLACEHOLDER_SERVICES = {"<service>", "my.radio", "my.telegram", "my.service", "Claude Code-credentials"}
GROUP_ID = re.compile(r"-100\d{7,}")
USER_ID = re.compile(r"""(?:owner_chat_id|user_id|from_id|chat_id|members?|OWNER\w*|["']id["']|\bid)["']?\s*[:=]\s*"""
                     r"""\{?\s*["']?(-?\d{6,})""")
# Every Telegram group or user id in the tree is a made-up placeholder; a new one must be added on purpose.
PLACEHOLDER_IDS = {
    "-1000000000", "-1001111111111", "-1001234567", "-10012345678", "-100123456789", "-1001234567890",
    "-1001999999999", "-1002222222222", "-1003000000000", "-1003000000099", "-1005550000", "-1005550001",
    "-1005555555555", "-1009876543210", "-1009999999999", "-100999", "-100555", "-1005555", "-100777", "-1001234",
    "123456789", "1234567890", "987654321", "1111111111", "2000000000", "9999999999", "4242424242",
    "1001111111111", "445566", "888123",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
EMAIL_OK = re.compile(r"@(?:[a-z0-9-]+\.)*example\.(?:com|org|net|test)$|^git@github\.com$|^noreply@anthropic\.com$",
                      re.I)


def _private_terms():
    try:
        lines = PRIVATE_TERMS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    terms = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return re.compile("|".join(f"(?:{t})" for t in terms), re.I) if terms else None


# Session ids (UUID-shaped) are private run details. A made-up id is obvious: four or more identical characters in a
# row ("0000", "1111"), or one of the few documented sample ids below (a public OAuth client id of the Claude Code
# sign-in page, and two classic ascending samples).
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
SAMPLE_UUIDS = {
    "9d1c250a-e61b-44d9-88ed-5944d1962f5e",  # the public client id in Claude's sign-in link, used in sample links
    "2f0e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b", "1f0e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",  # ascending samples
    "ab12cd34-ef56-7890-ab12-cd34ef567890",
}
# A comment or docstring that dates a note (2020s year-month-day, or day.month.year) tells private history; the code
# explains itself without it. API version strings such as "oauth-2025-04-20" are preceded by a word character or a dash.
DATE_NOTE = re.compile(r"(?<![\w-])202\d-\d\d-\d\d(?![\dT])|(?<!\d)\d{1,2}\.\d{1,2}\.202\d\b")
# A request quoted as something a person asked for or reported ("the owner asked for ... \"...\"").
ATTRIBUTED_QUOTE = re.compile(r"\((?:the )?owner(?:'s)? (?:report|request|asked|said|wrote)[^)\n]{0,60}[\"“]"
                              r"|\bowner (?:wrote|said|asked)\b[^\"\n]{0,60}\([\"“]", re.I)


def check(kind: str, line: str, following: str = "", name: str = ""):
    """The matches of one class in one line ('following' is the next line, for multi-line private keys; `name` is the
    file, which only the dated-comment check looks at: it applies to the code under src/)."""
    if kind == "session ids":
        return [u for u in UUID.findall(line) if u.lower() not in SAMPLE_UUIDS and not re.search(r"([0-9a-f])\1{3,}", u, re.I)]
    if kind == "dated comments":
        return DATE_NOTE.findall(line) if (name.startswith("src/") or not name) else []
    if kind == "attributed quotes":
        return [m.group(0) for m in ATTRIBUTED_QUOTE.finditer(line)]
    if kind in PATTERNS:
        return [m.group(0) for m in PATTERNS[kind].finditer(line)]
    if kind == "credentials":
        return [m.group(0)[:12] + "..." for m in CREDENTIAL.finditer(line)
                if not _fake_secret(m.group(1) or m.group(2))]
    if kind == "private keys":
        return ["private key"] if PEM_BEGIN.search(line) and (
            PEM_BODY.search(line[PEM_BEGIN.search(line).end():].replace("\\n", "\n").split("\n", 1)[-1])
            or PEM_BODY.search(following)) else []
    if kind == "phone numbers":
        return [m.group(0) for m in PHONE.finditer(line) if not _fake_phone(m.group(0))]
    if kind == "cloud project ids":
        found = [next(g for g in m.groups() if g) for m in PROJECT.finditer(line)]
        return [p for p in found if p not in PLACEHOLDER_PROJECTS and "{" not in p and p != "example"]
    if kind == "private URLs":
        return [h for h in URL.findall(line) if h.lower() not in PUBLIC_HOSTS and not HOST_OK.search(h)]
    if kind == "keychain service names":
        return [s for m in KEYCHAIN.finditer(line) for s in m.groups() if s and s not in PLACEHOLDER_SERVICES]
    if kind == "telegram ids":
        return [i for i in GROUP_ID.findall(line) + USER_ID.findall(line) if i not in PLACEHOLDER_IDS]
    if kind == "emails":
        return [m.group(0) for m in EMAIL.finditer(line) if not EMAIL_OK.search(m.group(0))]
    raise KeyError(kind)


KINDS = sorted(list(PATTERNS) + ["credentials", "private keys", "phone numbers", "cloud project ids",
                                 "private URLs", "keychain service names", "telegram ids", "emails",
                                 "session ids", "dated comments", "attributed quotes"])


@pytest.mark.parametrize("kind", KINDS)
def test_tree_has_no(kind):
    hits = []
    for name, text in FILES:
        if name == SELF:
            continue
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            found = check(kind, line, lines[number] if number < len(lines) else "", name)
            hits += [f"{name}:{number}: {f}" for f in found]
    assert not hits, kind + ":\n" + "\n".join(hits)


def test_no_private_names_from_the_private_deny_list():
    """Names of private people, projects and places (the private deny-list); skipped where it is absent."""
    pattern = _private_terms()
    if pattern is None:
        pytest.skip(f"no private deny-list at {PRIVATE_TERMS}")
    hits = [f"{where}: {line.strip()[:80]}" for where, line in _lines(include_self=True) if pattern.search(line)]
    assert not hits, "\n".join(hits)


def test_scan_covers_every_tracked_text_file():
    names = {n for n, _ in FILES}
    assert {"README.md", "pyproject.toml", "LICENSE", SELF} <= names
    assert any(n.startswith("docs/") for n in names) and any(n.startswith(".github/") for n in names)
    assert not names & PRIVATE_ONLY and not any(n.startswith(PRIVATE_PREFIXES) for n in names)


def test_no_built_in_private_project_labels():
    from ctb import topic_names
    assert topic_names.HE_PROJECTS == {} and topic_names.HE_ABOUT == {}


def test_guard_catches_each_class():
    """Each sample is built from pieces, so this file itself never matches."""
    j = "".join
    caught = {
        "personal paths": j(["/Users", "/dana/code"]),
        "dated owner notes": j(["(owner, 20", "26-11-01)"]),
        "bot tokens": j(["7712", "34561:AA", "Qw3rTy9uIoP4aSdF6gHjK2lZxC8vBnM0q5"]),
        "credentials": j(["AI", "za", "Sy9Qw3rTy7uIoP4aSdF6gHjK2lZxC8vBnM1"]),
        "private keys": j(["-----BEGIN RSA PRIV", "ATE KEY-----\\nMIIEowIBAAKCAQEA7Qw3rTy9uIoP4aSdF6gHjK2lZxC8vBnM"]),
        "phone numbers": j(["+44 20 79", "46 0958"]),
        "cloud project ids": j(["projects/", "acme-prod-4821", "/locations"]),
        "private URLs": j(["https://", "build.acme-internal", ".io/x"]),
        "keychain service names": j(['["security", "find-generic-password", "-s", "', "dev.dana.bot", '"]']),
        "telegram ids": j(['"owner_chat_id": ', "31415", "92653"]),
        "emails": j(["dana@", "gmail.com"]),
        "session ids": j(["3ff570c0-", "a1b2-4c3d-9e8f-", "5a6b7c8d9e0f"]),
        "dated comments": j(["# fixed in ", "2026-", "11-02"]),
        "attributed quotes": j(["(owner report: ", '"the old keyboard ', 'keeps coming back")']),
    }
    assert set(caught) == set(KINDS)
    for kind, sample in caught.items():
        assert check(kind, sample), kind
    more = {
        "phone numbers": [j(["050-73", "12984"]), j(["(212) 78", "4-0396"]), j(["+1 415 8", "93 7712"])],
        "telegram ids": [j(['"from": {"id": ', "7139", "28456"]), j(["OWNER = ", "6203", "91847"]),
                         j(["-100", "4242424242"])],
        "credentials": [j(["gh", "p_", "Qw3rTy9uIoP4aSdF6gHjK2lZx"]), j(["sk-", "ant-", "api03-Qw3rTy9uIoP4aSdF6"]),
                        j(["xox", "b-", "48213-Qw3rTy9uIoP4aSdF"])],
        "cloud project ids": [j(['"gemini_vertex_project": "', "acme-tts-4821", '"']),
                              j(["https://", "qwertyuiopasdfghjklz", ".supabase.co"])],
    }
    for kind, samples in more.items():
        for sample in samples:
            assert check(kind, sample), (kind, sample)
    assert check("dated comments", j(["# reset on ", "3.11.", "2026"]), name="src/ctb/x.py")
    assert not check("dated comments", j(["# fixed in ", "2026-", "11-02"]), name="tests/test_x.py")  # src/ only
    assert not check("dated comments", '"oauth-2025-04-20"') and not check("dated comments", '"2026-01-01T12:00:00Z"')
    # ... and the obvious placeholders stay allowed.
    for kind, sample in {"phone numbers": "050-1234567", "credentials": "gh" + "p_" + "abcdef" * 4,
                         "telegram ids": '"owner_chat_id": 123456789', "emails": "alice@example.com",
                         "private URLs": "https://bot-api.example.test/x", "cloud project ids": "projects/{project}/x",
                         "keychain service names": '"-s", "<service>"',
                         "session ids": "a1b2c3d4-0000-4000-8000-000000000001"}.items():
        assert not check(kind, sample), (kind, sample)
    # The private deny-list format: one regular expression per line, comments ignored.
    assert re.compile("|".join(["(?:acme)", "(?:\\bwidget\\b)"]), re.I).search("Widget")


def test_readmes_have_the_first_run_essentials():
    for name, needles in (("README.md", ("Quick start (5 minutes)", "harnessgram setup", "BotFather checklist",
                                         "## Security model", "uv tool install git+https://github.com/harnessgram/harnessgram",
                                         "CC-BY-NC", "Gemma Terms", "<!-- Demo GIF")),
                          ("README.he.md", ("harnessgram setup", "BotFather", "CC-BY-NC", "Gemma Terms"))):
        text = (ROOT / name).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, (name, needle)
    # The Quick start sits above everything else a newcomer might read.
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert text.index("## Quick start") < text.index("## Requirements")
    assert "lingoline" not in text.split("## Quick start")[0].lower()

# Public release plan: Harnessgram

Private checklist of this repository. It is **not** part of the public export (step 5 leaves it out).

Goal: publish the bridge as a fresh public repository `harnessgram/harnessgram` with a fresh history (one
initial commit), then on PyPI as `harnessgram`. Nothing is public until the owner says yes at step 4.

Legend: **[agents]** can be done by agents without asking; **[owner]** needs the owner's explicit yes or the
owner's own account.

## State on 2026-11-11 (see also the readiness report kept outside this repository)

- Branch `hotfix/installed-20261110` is what runs on the maintainer's Mac and is `main` plus a few hundred
  commits (a strict fast-forward: `main` has no commit that the branch lacks, so the integration merge cannot
  conflict). One integration pull request carries it to `main`; the per-area draft pull requests are superseded
  by it and are closed when it merges.
- The GitHub organisation `harnessgram` and the empty, **private** repository `harnessgram/harnessgram` exist
  (staging). Nothing is public. PyPI `harnessgram` is free (PyPI and TestPyPI JSON API answer 404).
- Secrets audit: `gitleaks dir` on a dry-run export reports only made-up test values (redaction tests), and the
  history holds no real token formats. Nothing needs rotating.
- Private data: removed from the tree and guarded by `tests/test_public_hygiene.py`, which scans every tracked
  text file for personal paths, dated owner notes, tokens, other credentials, private keys, phone numbers, cloud
  project ids, private URLs, identifying Keychain service names, chat ids and emails, each with a self-test.
  Names of private people, projects and places live in a private deny-list outside the repository
  (`~/.config/harnessgram/private-terms.txt`, or `HARNESSGRAM_PRIVATE_TERMS`), which the test reads when present
  and skips elsewhere. The private history still has the names, which is why the export uses a fresh history.
- Licence: MIT (`LICENSE`, `pyproject.toml`).
- Reviews: independent reviews are recorded for the pull requests of the integration batch; the remaining
  release gates are the clean-machine acceptance run, the open audit items (privacy defaults, quota time zone,
  upgrade and uninstall completeness, terminal-wording safety) and the owner's go.

## Steps

### 1. Reviews [agents]

Independent review (a different agent than the writer) of every PR without one:

| PR | Subject | Review |
|---|---|---|
| #136 to #141, #143 | panel pin, inbox media, model/stop, radio, topic names, delivery, instant replies | done (Codex) |
| #146 | typed-yes approval framing | done (Codex gpt-6.1-sol, 3 rounds) |
| #142 | background-agent notices per topic | **needed** |
| #144 | session title follows the topic | **needed** |
| #145 | model picker for new sessions | **needed** |
| #147 | Hebrew-first panel redesign | **needed** |
| #148 | weekly-only quota | **needed** |
| #149 | tests never reach live tmux | **needed** |
| #150 | context button | **needed** |
| #151 | radio paid voice and chime | **needed** |
| #152 | panel: newest first, team view | **needed** |
| #153 | talk with a session through the radio | **needed** (voice in, framing: security-relevant) |
| #154 | translation never blocks | **needed** |
| this PR | release audit (privacy, defaults, docs) | **needed**, stronger reviewer (privacy) |

Plus a ChatGPT second opinion on this PR and on the README, per the owner's standing rule.

### 2. Integration batch into `main` [agents]

1. Fix every review finding as a commit on its own PR (or on `hotfix/installed-20261110` when the PR is
   already merged there).
2. Open one non-draft PR `hotfix/installed-20261110` -> `main` (it carries #136 to #154 and this audit);
   close #136 to #139 and #85 as superseded by it. One CI run on that PR.
3. Before installing the batch on the owner's Mac: add the owner's private project labels to his config as
   `topic_project_names` and `topic_project_about` (the built-in map is gone; the exact values are in the
   description of this audit's PR, never in the tree). `panel_button` needs nothing: the panel service is
   installed there, so the button stays on.
4. Merge after all checks pass on the exact head (the owner's standing merge rule).

### 3. Release candidate [agents]

On `main` after the merge:

```bash
nice -n 10 uv run --with pytest --with-editable . python -m pytest -q -p no:cacheprovider --ignore=tests/test_live.py
gitleaks dir . --redact --no-banner
uv build && uvx twine check dist/*
```

Then a stranger test in a clean macOS user account or a fresh Linux VM (no owner config): install from the
wheel, `harnessgram setup` with a throwaway bot, check English menus, `harnessgram doctor` (every optional
feature "off", the Full Disk Access line on macOS), one mirrored reply, one typed reply, one voice note with
voice notes off (polite "off" answer), and that no panel button or radio appears.

Also check the off switch there: with `"mirror": false` in the config, nothing is sent, hooks do nothing, the
Cockpit answers 503 and `harnessgram pair` is refused; with the key removed (or no config file) everything works
again without a restart, and a message sent to the bot while it was off is not delivered.

Screenshots for the README (topics, a reply, a voice note, the panel) come from that test account only.

### 4. Go / no-go [owner]

One question to the owner with the recommendation "yes": publish `harnessgram` publicly (GitHub + PyPI)
under the MIT licence. This is a public publication, so it waits for his explicit yes.

### 5. Create the organisation and the repository [owner]

1. Create the GitHub organisation `harnessgram` (owner account; free plan).
2. Create the empty public repository `harnessgram/harnessgram` (no README, no licence: they come from the
   export). Turn on private vulnerability reporting (Settings > Security) so `SECURITY.md` works.
3. Optionally invite the agents' GitHub account as a maintainer so steps 6 and 8 need no further clicks.

### 6. Fresh-history export [agents, after step 5]

From the merged `main`, never from a working tree:

```bash
REV=$(git rev-parse origin/main)
OUT=$(mktemp -d)/harnessgram
mkdir -p "$OUT" && git archive "$REV" | tar -x -C "$OUT"
rm -rf "$OUT/.codex" "$OUT/docs/RELEASE_PLAN.md" "$OUT/uv.lock"
cd "$OUT" && git init -b main && git add -A
gitleaks dir . --redact --no-banner            # must report nothing but the known test fixtures
# on the owner's Mac, so the private deny-list is read; the deny-list test must pass, not skip
uv run --with pytest --with-editable . python -m pytest -q -rs tests/test_public_hygiene.py
git -c user.name="Harnessgram contributors" -c user.email="noreply@example.com" commit -m "Harnessgram 0.1.0"
```

The push to `harnessgram/harnessgram` is part of the publication the owner approved at step 4; agents do it
only after that yes, then tag `v0.1.0`.

### 7. PyPI [owner, once]

1. A PyPI account for the project (owner) with two-factor authentication.
2. A "pending publisher" for `harnessgram` with GitHub trusted publishing (repository `harnessgram/harnessgram`,
   workflow `release.yml`): no token is ever stored.

Then **[agents]** add `.github/workflows/release.yml` (build + publish on a `v*` tag through trusted
publishing) to the public repository and publish 0.1.0.

### 8. Announcement [owner]

Agents draft (English, with a short Hebrew version): a GitHub release note, a short post (what it does, the
security model in three lines, the install command, a GIF), and a list of places (Claude Code community
channels, r/ClaudeAI, Hacker News "Show HN"). Each post is public, so each waits for the owner's yes.

### 9. After the release [agents]

- Point the owner's install at the public repository (`uv tool install harnessgram`), keep his private
  config as it is.
- The private repository stays the development home for owner-only work, or development moves to the public
  repository with the private one archived: decide at step 4 (recommended: develop in public, keep private
  configuration only on the Mac).
- Watch issues and security reports on the public repository.

## Known limits to state in the release notes

- The team radio is macOS-only and its spoken replies are Hebrew-only for now.
- The bot's menus are English and Hebrew; translation of replies supports any language.
- A hand-written config without `target_language` reads as Hebrew (kept for older installs); `harnessgram
  setup` always writes it (English by default).

# Real screens from Claude Code 2.1.314

Captured with `tmux capture-pane -p -J` from the real CLI binary running in a private tmux server (explicit
`-S` socket, `TMUX`/`TMUX_PANE` unset), with a fresh `CLAUDE_CONFIG_DIR` and `HOME`, against a local fake API
that returns scripted tool calls. No real account, settings or network.

| file | screen |
|---|---|
| perm.txt | Bash permission prompt (`touch lab-file.txt`) |
| small.txt | the same prompt in a 12-row window: the header has scrolled off |
| plan.txt | "Exit plan mode?" (no key-hint row) |
| webfetch.txt | WebFetch permission (no key-hint row) |
| single.txt | AskUserQuestion, one question, with Other |
| multi.txt, multi_ticked.txt | AskUserQuestion multi-select with the unnumbered Submit row (ticked: 1 and 3, selector on Submit) |
| multi_next.txt | a multi-select that is not the last question: the unnumbered row is `Next`, not `Submit` |
| multiq.txt | AskUserQuestion with two questions (tab row) |
| review.txt | "Ready to submit your answers?" (no key-hint row) |
| preview.txt | a question with option previews (hint row has "n to add notes") |
| trust.txt | the trust-folder screen (unnumbered Yes/No rows) |
| exit_background.txt | "Background work is running" after /exit with two live `sleep` shells (real capture, fake API, 2.1.314) |
| exit_background_10.txt | the same dialog listing ten shells (typed from exit_background.txt, rows repeated) |
| exit_background_after_escape.txt | the same session one second after Escape: back at the prompt, `/exit` left in the history |
| footer_shells.txt | the idle prompt box with the footer `manual mode on · 2 shells · ↓ to manage` (real capture, same session) |
| footer_agent_list.txt | NOT a capture: the incident's bypass footer with a long agent list under it, typed from the 2026-11-02 report |

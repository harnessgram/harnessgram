"""A new session's topic name: project label or repository, never a worktree id or a markup tag."""
from ctb import topics


def test_a_claude_worktree_is_named_after_its_repository():
    name = topics.name_for_task("/work/orbit-rc/.claude/worktrees/bridge-cse_014abc", "check git status")
    assert name == "orbit-rc: check git status"


def test_markup_tags_in_the_first_prompt_are_dropped():
    name = topics.name_for_task("/work/orbit", '<wake reason="mention" from="x">check git status</wake>')
    assert name == "orbit: check git status"


def test_a_project_label_wins_over_the_folder():
    assert topics.name_for_task("/work/orbit-rc", "fix the login", "מסלול") == "מסלול: fix the login"


def test_plain_folders_are_unchanged():
    assert topics.name_for_task("/work/project", "one two three four five six") == "project: one two three four five"

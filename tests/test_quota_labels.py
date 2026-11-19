"""Quota rows in the Cockpit and the iPhone app never show two accounts under the same name."""
from ctb import panel


def test_duplicate_labels_get_the_account_name(monkeypatch):
    mails = {"main": "owner@example.com", "account12": "owner@example.com", "work": "work@example.com"}
    monkeypatch.setattr(panel.accounts, "label", lambda n: mails[n].split("@")[0])
    got = panel.quota_labels(["main", "account12", "work"])
    assert got == {"main": "owner (main)", "account12": "owner (account12)", "work": "work"}
    assert len(set(got.values())) == 3


def test_unique_labels_unchanged(monkeypatch):
    monkeypatch.setattr(panel.accounts, "label", lambda n: n)
    assert panel.quota_labels(["a", "b"]) == {"a": "a", "b": "b"}

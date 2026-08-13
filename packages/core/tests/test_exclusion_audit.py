"""
`.ocignore` exclusion auditing (SCAN-OCIGNORE, finding 84).

The defect: `.ocignore` is repo-owned with no restriction, and directory-level
excludes were pruned with **no trace** — the file branch recorded a skip, the
directory branch just `continue`d. So an audited party could exclude the exact
tree under audit and produce a clean `severity=NONE` report **byte-identical**
to one from a genuinely AI-free repository.

`config_hash` already made the config tamper-*evident*. These tests cover what
it did not: making an exclusion *visible* and *suspicious*.
"""

from __future__ import annotations

import pytest
from opencomplai_core.scanner.exclusion_audit import (
    audit_exclusions,
    flag_ai_related_patterns,
)


def hints(patterns, baseline=None) -> set[str]:
    return {f.pattern for f in flag_ai_related_patterns(patterns, baseline)}


@pytest.mark.parametrize(
    "pattern",
    [
        "src/ml/",
        "ai/",
        "**/llm/**",
        "models/",
        "inference/",
        "prompts/",
        "app/agents/",
        "lib/embeddings/",
        "vendor/langchain/",
        "biometric/",
    ],
)
def test_ai_related_exclusions_are_flagged(pattern: str):
    assert pattern in hints([pattern])


@pytest.mark.parametrize(
    "pattern",
    ["node_modules/", "dist/", "*.log", "build/", "coverage/", "docs/"],
)
def test_ordinary_exclusions_are_not_flagged(pattern: str):
    assert hints([pattern]) == set()


def test_hints_do_not_fire_on_substrings():
    """
    'ai' inside 'chain' or 'ml' inside 'html' would make the flag useless by
    firing on almost everything, and a flag that always fires is ignored.
    """
    assert hints(["*.html", "src/chain/", "mailers/", "normalize/"]) == set()


def test_comments_and_negations_are_not_exclusions():
    # A negation adds coverage back, so flagging it would be exactly backwards.
    assert hints(["# ignore the ml dir", "!src/ml/keep.py"]) == set()


def test_newly_added_exclusions_are_marked_against_a_baseline():
    flags = flag_ai_related_patterns(
        ["node_modules/", "src/ml/"], baseline_patterns=["node_modules/"]
    )

    assert len(flags) == 1
    assert flags[0].pattern == "src/ml/"
    assert flags[0].newly_added is True
    assert "newly-added" in flags[0].describe()


def test_a_long_standing_exclusion_is_flagged_but_not_marked_new():
    flags = flag_ai_related_patterns(["src/ml/"], baseline_patterns=["src/ml/"])

    assert len(flags) == 1
    assert flags[0].newly_added is False
    assert "newly-added" not in flags[0].describe()


def test_absent_baseline_does_not_mark_everything_new():
    """
    Treating "no baseline" as "all new" would flood a first scan and train
    reviewers to ignore the flags entirely.
    """
    flags = flag_ai_related_patterns(["src/ml/"], baseline_patterns=None)

    assert len(flags) == 1
    assert flags[0].newly_added is False


def test_flag_names_the_hint_that_fired():
    """A reviewer must be able to judge the match, not just trust it."""
    flags = flag_ai_related_patterns(["services/inference/"])

    assert flags[0].matched_hint == "inference"


def test_audit_collects_excluded_directories_uniquely():
    audit = audit_exclusions(
        ["src/ml/"], excluded_directories=["src/ml", "src/ml", "vendor"]
    )

    assert audit.excluded_directories == ["src/ml", "vendor"]
    assert audit.has_findings


def test_audit_of_a_clean_config_reports_nothing():
    audit = audit_exclusions(["node_modules/", "dist/"], excluded_directories=[])

    assert not audit.has_findings
    assert audit.summary() == []
    assert audit.newly_added == []


def test_empty_input_is_safe():
    audit = audit_exclusions([], excluded_directories=None)

    assert not audit.has_findings
    assert audit.excluded_directories == []


# --- end to end through run_scan -------------------------------------------


def _repo_with_hidden_ai(tmp_path, ocignore: str):
    repo = tmp_path / "repo"
    (repo / "src" / "ml").mkdir(parents=True)
    (repo / "src" / "ml" / "infer.py").write_text(
        "import openai\nopenai.chat.completions.create()\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# app\n", encoding="utf-8")
    (repo / ".ocignore").write_text(ocignore, encoding="utf-8")
    return repo


def test_excluding_an_ai_directory_is_recorded_in_the_report(tmp_path):
    """
    The core defect. Excluding the tree under audit used to yield a report
    indistinguishable from a genuinely AI-free repository.
    """
    from opencomplai_core.scan_engine import run_scan

    repo = _repo_with_hidden_ai(tmp_path, "src/ml/\n")

    report = run_scan(
        system_id="sys-1",
        commit_ref="HEAD",
        repo_root=repo,
        declared_purpose="internal tooling",
    )

    # The subtree was pruned -- but the report now says so.
    assert any("src/ml" in d for d in report.excluded_directories)
    assert report.exclusion_flags
    assert any("src/ml" in flag for flag in report.exclusion_flags)


def test_an_ordinary_exclusion_produces_no_flag(tmp_path):
    from opencomplai_core.scan_engine import run_scan

    repo = tmp_path / "repo"
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "i.js").write_text("//x\n", encoding="utf-8")
    (repo / "app.py").write_text("print(1)\n", encoding="utf-8")
    (repo / ".ocignore").write_text("node_modules/\n", encoding="utf-8")

    report = run_scan(
        system_id="sys-1",
        commit_ref="HEAD",
        repo_root=repo,
        declared_purpose="internal tooling",
    )

    assert report.exclusion_flags == []
    assert any("node_modules" in d for d in report.excluded_directories)


def test_a_newly_added_ai_exclusion_is_labelled_against_a_baseline(tmp_path):
    from opencomplai_core.scan_engine import run_scan

    repo = _repo_with_hidden_ai(tmp_path, "src/ml/\n")

    report = run_scan(
        system_id="sys-1",
        commit_ref="HEAD",
        repo_root=repo,
        declared_purpose="internal tooling",
        baseline_ignore_patterns=["node_modules/"],
    )

    assert any("newly-added" in flag for flag in report.exclusion_flags)


def test_exclusion_flags_are_advisory_and_do_not_change_severity(tmp_path):
    """
    A scanner cannot tell a legitimate `vendor/` exclusion from a self-serving
    `src/ml/` one. Flagging informs a reviewer; it must not silently fail a
    build on a heuristic.
    """
    from opencomplai_core.scan_engine import run_scan

    repo = _repo_with_hidden_ai(tmp_path, "src/ml/\n")

    report = run_scan(
        system_id="sys-1",
        commit_ref="HEAD",
        repo_root=repo,
        declared_purpose="internal tooling",
    )

    assert report.exclusion_flags
    assert report.scan_errors == []
    assert report.detector_errors == []

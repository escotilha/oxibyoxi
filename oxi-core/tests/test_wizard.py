"""Tests for oxi_core.wizard — `oxi init` scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxi_core.wizard import (
    WizardAnswers,
    _substitute,
    collect_answers,
    default_template_root,
    run,
    scaffold,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _scripted_input(responses: list[str]):
    """Return an input callable that yields each response in order."""
    it = iter(responses)
    def _input(_prompt):
        return next(it)
    return _input


def _sample_answers(**overrides) -> WizardAnswers:
    base = dict(
        project_name="my project",
        adapter_slug="my-project",
        package_suffix="my_project",
        github_repo="owner/my-project",
        repo_root="/tmp/my-project",
        roadmap_location="roadmap.md",
        plan_tier="standard",
        budget_soft_warn=5.0,
        budget_hard_cap=20.0,
        budget_per_task_opus=2.0,
        budget_per_task_sonnet=0.5,
        max_concurrent=3,
        auto_merge=False,
    )
    base.update(overrides)
    return WizardAnswers(**base)


# ---------------------------------------------------------------------------
# collect_answers — validation loops
# ---------------------------------------------------------------------------


def test_collect_happy_path_with_all_defaults(capsys):
    """Operator hits enter for every default-offered prompt."""
    inputs = [
        "my project",   # project name
        "",             # adapter slug (default)
        "owner/repo",   # github repo
        "",             # repo root (default = cwd)
        "",             # roadmap location (default)
        "",             # plan tier (default)
        "",             # soft warn (default)
        "",             # hard cap (default)
        "",             # per-task opus (default)
        "",             # per-task sonnet (default)
        "",             # max concurrent (default)
        "",             # auto_merge (default = N)
    ]
    answers = collect_answers(input_fn=_scripted_input(inputs))
    assert answers.project_name == "my project"
    assert answers.adapter_slug == "my-project"
    assert answers.package_suffix == "my_project"
    assert answers.github_repo == "owner/repo"
    assert answers.roadmap_location == "roadmap.md"
    assert answers.plan_tier == "standard"
    assert answers.budget_soft_warn == 5.0
    assert answers.budget_hard_cap == 20.0
    assert answers.max_concurrent == 3
    assert answers.auto_merge is False


def test_empty_project_name_reprompts():
    inputs = [
        "",             # reject empty
        "real name",    # accept
        "",             # adapter slug default
        "owner/repo",
        "/tmp/x",
        "",             # roadmap default
        "",             # plan tier default
        "", "", "", "", "", "",
    ]
    answers = collect_answers(input_fn=_scripted_input(inputs))
    assert answers.project_name == "real name"


def test_invalid_github_repo_reprompts():
    inputs = [
        "proj",
        "",
        "not-a-repo",   # rejected
        "owner/name",   # accepted
        "/tmp/x",
        "",
        "",
        "", "", "", "", "", "",
    ]
    answers = collect_answers(input_fn=_scripted_input(inputs))
    assert answers.github_repo == "owner/name"


def test_relative_repo_root_reprompts():
    inputs = [
        "proj",
        "",
        "owner/name",
        "relative/path",   # rejected
        "/tmp/absolute",   # accepted
        "",
        "",
        "", "", "", "", "", "",
    ]
    answers = collect_answers(input_fn=_scripted_input(inputs))
    assert answers.repo_root == "/tmp/absolute"


def test_invalid_adapter_slug_reprompts():
    inputs = [
        "proj",
        "Bad_Slug",     # uppercase + underscore → rejected
        "good-slug",    # accepted
        "owner/name",
        "/tmp/x",
        "", "",
        "", "", "", "", "", "",
    ]
    answers = collect_answers(input_fn=_scripted_input(inputs))
    assert answers.adapter_slug == "good-slug"
    assert answers.package_suffix == "good_slug"


def test_auto_merge_yes():
    inputs = [
        "proj",
        "",
        "owner/name",
        "/tmp/x",
        "", "",
        "", "", "", "",
        "",       # max_concurrent
        "y",      # auto_merge
    ]
    answers = collect_answers(input_fn=_scripted_input(inputs))
    assert answers.auto_merge is True


def test_non_positive_max_concurrent_reprompts():
    inputs = [
        "proj",
        "",
        "owner/name",
        "/tmp/x",
        "", "",
        "", "", "", "",
        "0",      # rejected
        "4",      # accepted
        "",       # auto_merge default
    ]
    answers = collect_answers(input_fn=_scripted_input(inputs))
    assert answers.max_concurrent == 4


# ---------------------------------------------------------------------------
# _substitute
# ---------------------------------------------------------------------------


def test_substitute_replaces_placeholders():
    text = "Hello {{PROJECT_NAME}}, budget={{BUDGET_HARD_CAP}}"
    out = _substitute(text, _sample_answers(budget_hard_cap=15.0))
    assert "my project" in out
    assert "15.0" in out
    assert "{{" not in out


def test_substitute_handles_auto_merge_bool():
    text = "auto_merge={{AUTO_MERGE}}"
    assert _substitute(text, _sample_answers(auto_merge=True)) == "auto_merge=True"
    assert _substitute(text, _sample_answers(auto_merge=False)) == "auto_merge=False"


def test_substitute_injects_today_in_iso_format():
    import re
    text = "generated {{TODAY}}"
    out = _substitute(text, _sample_answers())
    assert re.search(r"\d{4}-\d{2}-\d{2}", out)


# ---------------------------------------------------------------------------
# scaffold — writes files into a destination directory
# ---------------------------------------------------------------------------


def test_scaffold_writes_all_template_files(tmp_path: Path):
    template = default_template_root()
    dest = tmp_path / "out"
    answers = _sample_answers()
    written = scaffold(answers, dest, template_root=template)

    # Key expected files exist.
    assert (dest / "pyproject.toml").exists()
    assert (dest / "README.md").exists()
    pkg_dir = dest / "src" / f"oxi_adapter_{answers.package_suffix}"
    assert (pkg_dir / "__init__.py").exists()
    assert (pkg_dir / "adapter.py").exists()

    # Placeholders gone.
    for path in written:
        content = path.read_text(encoding="utf-8")
        assert "{{" not in content, f"{path} still has placeholders"


def test_scaffold_substitutes_project_name_in_adapter(tmp_path: Path):
    template = default_template_root()
    dest = tmp_path / "out"
    answers = _sample_answers(
        project_name="alphabeta",
        adapter_slug="alphabeta",
        package_suffix="alphabeta",
        github_repo="owner/alphabeta",
    )
    scaffold(answers, dest, template_root=template)

    adapter_py = dest / "src" / "oxi_adapter_alphabeta" / "adapter.py"
    content = adapter_py.read_text()
    assert 'instance_name="alphabeta"' in content
    assert 'return "owner/alphabeta"' in content


def test_scaffold_refuses_nonempty_destination(tmp_path: Path):
    template = default_template_root()
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "preexisting.txt").write_text("x")
    with pytest.raises(FileExistsError):
        scaffold(_sample_answers(), dest, template_root=template)


def test_scaffold_force_overwrites(tmp_path: Path):
    template = default_template_root()
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "preexisting.txt").write_text("x")
    # Should not raise.
    scaffold(_sample_answers(), dest, template_root=template, force=True)
    assert (dest / "pyproject.toml").exists()


def test_scaffolded_adapter_is_importable(tmp_path: Path):
    """The scaffolded adapter is syntactically valid Python that can
    actually be imported. Catches broken templates.
    """
    import sys

    template = default_template_root()
    dest = tmp_path / "out"
    answers = _sample_answers(
        project_name="importme",
        adapter_slug="importme",
        package_suffix="importme",
        repo_root="/tmp/importme",
    )
    scaffold(answers, dest, template_root=template)

    # Put the package on sys.path and import.
    pkg_parent = dest / "src"
    sys.path.insert(0, str(pkg_parent))
    try:
        # Clean any stale import cache.
        for name in list(sys.modules):
            if name.startswith("oxi_adapter_importme"):
                del sys.modules[name]
        import importlib
        module = importlib.import_module("oxi_adapter_importme")
        assert hasattr(module, "Adapter")
        adapter = module.Adapter()
        # Every protocol method returns a real value.
        assert adapter.naming().instance_name == "importme"
        assert adapter.github_repo() == "owner/my-project"  # from sample
        assert adapter.plan_tier() == "standard"
    finally:
        sys.path.remove(str(pkg_parent))


# ---------------------------------------------------------------------------
# run — end-to-end
# ---------------------------------------------------------------------------


def test_run_end_to_end(tmp_path: Path, capsys):
    inputs = [
        "demo",
        "",
        "owner/demo",
        str(tmp_path / "repo"),
        "",
        "",
        "", "", "", "",
        "",
        "",
    ]
    dest = tmp_path / "adapter-out"
    answers = run(
        dest,
        input_fn=_scripted_input(inputs),
    )
    assert answers.project_name == "demo"
    assert (dest / "src" / "oxi_adapter_demo" / "adapter.py").exists()

    # "Next steps" printed.
    captured = capsys.readouterr()
    assert "Next steps" in captured.out
    assert "pip install -e ." in captured.out


# ---------------------------------------------------------------------------
# default_template_root
# ---------------------------------------------------------------------------


def test_default_template_root_finds_template():
    path = default_template_root()
    assert path.is_dir()
    # Must contain at least the pyproject.toml.tmpl.
    assert (path / "pyproject.toml.tmpl").is_file()


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


def test_wizard_answers_is_frozen():
    from dataclasses import FrozenInstanceError
    answers = _sample_answers()
    with pytest.raises(FrozenInstanceError):
        answers.project_name = "other"  # type: ignore[misc]

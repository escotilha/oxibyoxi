"""`oxi init` — 8-step interactive scaffolder for a new adapter.

Produces a working adapter package at a user-chosen path. The output
is a real installable Python package with a real `Adapter` class; the
user can `pip install -e .` it immediately and start using oxi.

Design notes:

- **No project-specific strings in the template.** The template files
  ship with `{{PLACEHOLDER}}` tokens; the wizard substitutes them at
  runtime based on operator answers. Leak-lint is satisfied because
  the placeholders don't match any forbidden pattern.
- **Fail-closed validation.** Plan tier can't be empty. GitHub repo
  must be `owner/name`. Paths must be absolute. If the validation
  fails, the wizard prompts again rather than writing a broken adapter.
- **Idempotent.** Running `oxi init` into a directory that already
  contains an adapter refuses to overwrite unless `--force` is passed.
- **Not async.** The wizard is a straight sequence of `input()` calls.
  Async buys nothing for interactive prompts.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Template source — shipped alongside oxi-core. Wizard reads these at
# runtime and substitutes placeholders. The adapters/_template/
# directory on disk is the single source of truth.
_TEMPLATE_MARKER = ".template"


@dataclass(frozen=True)
class WizardAnswers:
    """Everything the wizard collects. Each field is validated."""

    project_name: str
    adapter_slug: str
    package_suffix: str
    github_repo: str
    repo_root: str
    roadmap_location: str
    plan_tier: str
    budget_soft_warn: float
    budget_hard_cap: float
    budget_per_task_opus: float
    budget_per_task_sonnet: float
    max_concurrent: int
    auto_merge: bool


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _valid_project_name(name: str) -> bool:
    return bool(name.strip())


def _valid_adapter_slug(slug: str) -> bool:
    """A slug that works as a PyPI package name suffix.

    Lowercase letters, digits, hyphens. No leading/trailing hyphen.
    """
    return bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug))


def _valid_github_repo(repo: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", repo))


def _valid_abs_path(path: str) -> bool:
    return path.startswith("/") and not any(c in path for c in ("\n", "\x00"))


def _valid_plan_tier(tier: str) -> bool:
    return bool(tier.strip())


def _valid_positive_float(value: str) -> bool:
    try:
        return float(value) >= 0.0
    except ValueError:
        return False


def _valid_positive_int(value: str) -> bool:
    try:
        return int(value) >= 1
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _prompt(
    question: str,
    *,
    default: str | None = None,
    validator=lambda v: True,
    error: str = "invalid value",
    input_fn=input,
) -> str:
    """Ask `question`; accept default on empty; loop until valid."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = input_fn(f"{question}{suffix}: ").strip()
        if not answer and default is not None:
            answer = default
        if validator(answer):
            return answer
        sys.stderr.write(f"oxi init: {error}\n")


def _prompt_bool(
    question: str,
    *,
    default: bool = False,
    input_fn=input,
) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        answer = input_fn(f"{question} [{default_label}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        sys.stderr.write("oxi init: answer y or n\n")


def collect_answers(input_fn=input) -> WizardAnswers:
    """Interactive prompting. All validation is here.

    Wizard steps (8):

    1. Project name — human-readable label for the instance.
    2. Adapter slug — PyPI-safe package suffix (e.g. "my-project").
    3. GitHub repo — owner/name that this adapter ships PRs to.
    4. Repo root — absolute path to the local checkout.
    5. Roadmap location — relative path inside the repo.
    6. Plan tier — e.g. "max_20x" or "standard".
    7. Budget — per-task and daily caps.
    8. Dispatch policy — max_concurrent + auto_merge toggle.
    """
    print("oxi init — 8-step adapter scaffold")
    print("=" * 40)
    print()

    project_name = _prompt(
        "Project name (what's your project called?)",
        validator=_valid_project_name,
        error="non-empty string required",
        input_fn=input_fn,
    )

    # Default adapter slug derived from project name.
    default_slug = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
    adapter_slug = _prompt(
        "Adapter slug (lowercase, hyphens; used in PyPI name)",
        default=default_slug,
        validator=_valid_adapter_slug,
        error="lowercase letters, digits, hyphens; no leading/trailing hyphen",
        input_fn=input_fn,
    )
    package_suffix = adapter_slug.replace("-", "_")

    github_repo = _prompt(
        "GitHub repo (owner/name)",
        validator=_valid_github_repo,
        error="must match owner/name",
        input_fn=input_fn,
    )

    repo_root = _prompt(
        "Repo root (absolute path to local checkout)",
        default=str(Path.cwd()),
        validator=_valid_abs_path,
        error="must be an absolute path",
        input_fn=input_fn,
    )

    roadmap_location = _prompt(
        "Roadmap location (relative path inside the repo)",
        default="roadmap.md",
        validator=lambda v: bool(v.strip()) and not v.startswith("/"),
        error="relative path required (e.g. roadmap.md or docs/roadmap.md)",
        input_fn=input_fn,
    )

    plan_tier = _prompt(
        "Claude plan tier (e.g. standard, max_5x, max_20x)",
        default="standard",
        validator=_valid_plan_tier,
        error="non-empty string required",
        input_fn=input_fn,
    )

    print("\nBudget caps (USD):")
    budget_soft_warn = float(_prompt(
        "  daily soft warn",
        default="5.0",
        validator=_valid_positive_float,
        input_fn=input_fn,
    ))
    budget_hard_cap = float(_prompt(
        "  daily hard cap",
        default="20.0",
        validator=_valid_positive_float,
        input_fn=input_fn,
    ))
    budget_per_task_opus = float(_prompt(
        "  per-task Opus cap",
        default="2.0",
        validator=_valid_positive_float,
        input_fn=input_fn,
    ))
    budget_per_task_sonnet = float(_prompt(
        "  per-task Sonnet cap",
        default="0.50",
        validator=_valid_positive_float,
        input_fn=input_fn,
    ))

    print("\nDispatch:")
    max_concurrent = int(_prompt(
        "  max concurrent dispatches",
        default="3",
        validator=_valid_positive_int,
        input_fn=input_fn,
    ))
    auto_merge = _prompt_bool(
        "  auto-merge approved PRs (requires a trusted critic)",
        default=False,
        input_fn=input_fn,
    )

    return WizardAnswers(
        project_name=project_name,
        adapter_slug=adapter_slug,
        package_suffix=package_suffix,
        github_repo=github_repo,
        repo_root=repo_root,
        roadmap_location=roadmap_location,
        plan_tier=plan_tier,
        budget_soft_warn=budget_soft_warn,
        budget_hard_cap=budget_hard_cap,
        budget_per_task_opus=budget_per_task_opus,
        budget_per_task_sonnet=budget_per_task_sonnet,
        max_concurrent=max_concurrent,
        auto_merge=auto_merge,
    )


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _substitute(text: str, answers: WizardAnswers) -> str:
    """Replace {{PLACEHOLDER}} tokens with the operator's answers."""
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    mapping = {
        "{{PROJECT_NAME}}": answers.project_name,
        "{{ADAPTER_SLUG}}": answers.adapter_slug,
        "{{PACKAGE_SUFFIX}}": answers.package_suffix,
        "{{GITHUB_REPO}}": answers.github_repo,
        "{{REPO_ROOT}}": answers.repo_root,
        "{{ROADMAP_LOCATION}}": answers.roadmap_location,
        "{{PLAN_TIER}}": answers.plan_tier,
        "{{BUDGET_SOFT_WARN}}": str(answers.budget_soft_warn),
        "{{BUDGET_HARD_CAP}}": str(answers.budget_hard_cap),
        "{{BUDGET_PER_TASK_OPUS}}": str(answers.budget_per_task_opus),
        "{{BUDGET_PER_TASK_SONNET}}": str(answers.budget_per_task_sonnet),
        "{{MAX_CONCURRENT}}": str(answers.max_concurrent),
        "{{AUTO_MERGE}}": "True" if answers.auto_merge else "False",
        "{{TODAY}}": today,
    }
    for placeholder, value in mapping.items():
        text = text.replace(placeholder, value)
    return text


def scaffold(
    answers: WizardAnswers,
    destination: Path,
    *,
    template_root: Path,
    force: bool = False,
) -> list[Path]:
    """Copy template into `destination`, substituting placeholders.

    Returns the list of files written. Refuses to overwrite an
    existing non-empty destination unless `force=True`.
    """
    if destination.exists() and any(destination.iterdir()):
        if not force:
            raise FileExistsError(
                f"destination {destination} is not empty; pass force=True to overwrite"
            )

    written: list[Path] = []
    pkg_dir_name = f"oxi_adapter_{answers.package_suffix}"

    for src_path in template_root.rglob("*"):
        if src_path.is_dir():
            continue
        rel = src_path.relative_to(template_root)

        # Rename the placeholder directory name to the real package.
        parts = tuple(
            pkg_dir_name if p == "oxi_adapter_TEMPLATE" else p
            for p in rel.parts
        )

        # Strip trailing ".tmpl" from file names; skip plain README.md
        # which is the template's own pre-gen notice.
        final_name = parts[-1]
        if final_name == "README.md":
            # This is the stub README. Skip — the .tmpl variant is
            # what we write.
            continue
        if final_name.endswith(".tmpl"):
            final_name = final_name[: -len(".tmpl")]

        out_parts = parts[:-1] + (final_name,)
        dest_path = destination.joinpath(*out_parts)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        content = src_path.read_text(encoding="utf-8")
        dest_path.write_text(_substitute(content, answers), encoding="utf-8")
        written.append(dest_path)

    return written


def default_template_root() -> Path:
    """Locate the template directory relative to this package.

    Installed via pip: the template directory is packaged alongside
    `oxi_core`. Editable install: adapters/_template is one level up
    from the source tree.
    """
    here = Path(__file__).resolve().parent
    # oxi-core/src/oxi_core/wizard.py → oxi-core/src → oxi-core → repo root
    for candidate in (
        here.parent.parent / "adapters" / "_template",
        here.parent.parent.parent / "adapters" / "_template",
        Path.cwd() / "adapters" / "_template",
    ):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "adapters/_template not found. If installed from PyPI this is a "
        "packaging bug; please open an issue."
    )


def run(
    destination: Path,
    *,
    template_root: Path | None = None,
    force: bool = False,
    input_fn=input,
) -> WizardAnswers:
    """Full flow: prompt → scaffold → print next steps.

    Returns the ``WizardAnswers`` so callers (tests) can assert on
    what was collected.
    """
    if template_root is None:
        template_root = default_template_root()

    answers = collect_answers(input_fn=input_fn)
    written = scaffold(
        answers, destination,
        template_root=template_root, force=force,
    )

    print()
    print(f"oxi init: scaffolded {len(written)} files into {destination}")
    print()
    print("Next steps:")
    print(f"  1. cd {destination}")
    print("  2. pip install -e .")
    print("  3. oxi status   # confirms the adapter loads")
    print()
    print("Edit src/oxi_adapter_" + answers.package_suffix + "/adapter.py to tune.")
    return answers


__all__ = [
    "WizardAnswers",
    "collect_answers",
    "default_template_root",
    "run",
    "scaffold",
]
_ = shutil  # reserved for a future --force cleanup path

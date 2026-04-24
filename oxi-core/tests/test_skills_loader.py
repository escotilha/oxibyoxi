"""Tests for oxi_core.v3.skills_loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxi_core.v3.skills_loader import (
    SkillParseError,
    iter_skill_metadata,
    load_skill_body,
    parse_skill_metadata,
)

_BASIC_SKILL = """---
name: my-skill
description: A simple skill for testing.
user-invocable: true
model: sonnet
---

# Body

Detailed instructions go here.
"""


_SKILL_WITH_TAGS_LIST = """---
name: tagged
description: Has tags as a YAML list.
tags:
  - alpha
  - beta
  - gamma
---

Body.
"""


_SKILL_WITH_TAGS_CSV = """---
name: csv-tags
description: Has tags as CSV.
tags: alpha, beta, gamma
---

Body.
"""


_SKILL_MISSING_FRONTMATTER = """# My skill

No frontmatter here.
"""


_SKILL_MISSING_NAME = """---
description: I have no name.
---

Body.
"""


_SKILL_WITH_STRING_QUOTES = """---
name: 'quoted-name'
description: "Double-quoted description."
---

Body.
"""


# ---------------------------------------------------------------------------
# parse_skill_metadata
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, content: str, name: str = "SKILL.md") -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


def test_parse_basic_skill(tmp_path: Path):
    path = _write(tmp_path, _BASIC_SKILL)
    md = parse_skill_metadata(path)
    assert md.name == "my-skill"
    assert md.description == "A simple skill for testing."
    assert md.user_invocable is True
    assert md.model == "sonnet"


def test_parse_tags_yaml_list(tmp_path: Path):
    path = _write(tmp_path, _SKILL_WITH_TAGS_LIST)
    md = parse_skill_metadata(path)
    assert md.tags == ("alpha", "beta", "gamma")


def test_parse_tags_csv(tmp_path: Path):
    path = _write(tmp_path, _SKILL_WITH_TAGS_CSV)
    md = parse_skill_metadata(path)
    assert md.tags == ("alpha", "beta", "gamma")


def test_missing_frontmatter_raises(tmp_path: Path):
    path = _write(tmp_path, _SKILL_MISSING_FRONTMATTER)
    with pytest.raises(SkillParseError, match="frontmatter"):
        parse_skill_metadata(path)


def test_missing_required_field_raises(tmp_path: Path):
    path = _write(tmp_path, _SKILL_MISSING_NAME)
    with pytest.raises(SkillParseError, match="name"):
        parse_skill_metadata(path)


def test_string_values_unquoted(tmp_path: Path):
    path = _write(tmp_path, _SKILL_WITH_STRING_QUOTES)
    md = parse_skill_metadata(path)
    assert md.name == "quoted-name"
    assert md.description == "Double-quoted description."


def test_user_invocable_defaults_false(tmp_path: Path):
    """Omitted user-invocable field → False."""
    content = "---\nname: x\ndescription: y\n---\n\nbody\n"
    path = _write(tmp_path, content)
    md = parse_skill_metadata(path)
    assert md.user_invocable is False


def test_hyphenated_keys_normalized_to_underscored(tmp_path: Path):
    """`user-invocable` becomes `user_invocable` in the dict."""
    path = _write(tmp_path, _BASIC_SKILL)
    md = parse_skill_metadata(path)
    assert md.raw["user_invocable"] is True


def test_root_property_returns_parent_dir(tmp_path: Path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    path = _write(skill_dir, _BASIC_SKILL)
    md = parse_skill_metadata(path)
    assert md.root == skill_dir


# ---------------------------------------------------------------------------
# iter_skill_metadata
# ---------------------------------------------------------------------------


def test_iter_finds_all_skills(tmp_path: Path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    _write(tmp_path / "alpha", _BASIC_SKILL)
    _write(tmp_path / "beta",
           "---\nname: beta-skill\ndescription: second.\n---\n\nBody.\n")
    names = {md.name for md in iter_skill_metadata(tmp_path)}
    assert names == {"my-skill", "beta-skill"}


def test_iter_skips_malformed(tmp_path: Path, caplog):
    (tmp_path / "good").mkdir()
    (tmp_path / "bad").mkdir()
    _write(tmp_path / "good", _BASIC_SKILL)
    _write(tmp_path / "bad", _SKILL_MISSING_FRONTMATTER)
    names = [md.name for md in iter_skill_metadata(tmp_path)]
    assert names == ["my-skill"]


def test_iter_missing_root_yields_nothing(tmp_path: Path):
    nonexistent = tmp_path / "does-not-exist"
    assert list(iter_skill_metadata(nonexistent)) == []


def test_iter_recurses(tmp_path: Path):
    """Nested skills directories (skills/foo/bar/SKILL.md) discovered."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    _write(nested, _BASIC_SKILL)
    names = [md.name for md in iter_skill_metadata(tmp_path)]
    assert names == ["my-skill"]


# ---------------------------------------------------------------------------
# load_skill_body
# ---------------------------------------------------------------------------


def test_load_skill_body(tmp_path: Path):
    path = _write(tmp_path, _BASIC_SKILL)
    body = load_skill_body(path)
    assert body.startswith("# Body")
    assert "Detailed instructions" in body


def test_load_skill_body_is_cached(tmp_path: Path):
    """Calling load_skill_body twice reads the file once."""
    path = _write(tmp_path, _BASIC_SKILL)
    load_skill_body.cache_clear()
    first = load_skill_body(path)
    # Overwrite on disk; cache should still return the first value.
    path.write_text("---\nname: x\ndescription: y\n---\n\nnew body\n")
    second = load_skill_body(path)
    assert first == second


# ---------------------------------------------------------------------------
# SkillMetadata dataclass
# ---------------------------------------------------------------------------


def test_metadata_is_frozen(tmp_path: Path):
    from dataclasses import FrozenInstanceError
    path = _write(tmp_path, _BASIC_SKILL)
    md = parse_skill_metadata(path)
    with pytest.raises(FrozenInstanceError):
        md.name = "other"  # type: ignore[misc]

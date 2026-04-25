"""Load SKILL.md files with progressive disclosure.

Claude Code skills store metadata in YAML frontmatter at the top of
each SKILL.md:

    ---
    name: my-skill
    description: One-line description shown in skill pickers.
    user-invocable: true
    model: sonnet
    ---

    # Body

    Full skill prose, tool examples, etc.

For the dispatch skill picker, oxi needs name + description on every
tick (cheap). For actually-invoked skills, it needs the body too
(expensive — may be multiple KB). This module loads them separately.

``iter_skill_metadata(root)`` walks the skills directory and yields
``SkillMetadata`` with just the frontmatter. Fast, safe to call
often.

``load_skill_body(path)`` reads a single skill's full body when
dispatch actually picks it. Cached via LRU for repeat picks within
one process.

No YAML dependency — we use a tiny frontmatter parser that handles
the subset Claude skills actually use (flat key-value, strings, bools,
lists of strings). Avoids pulling in PyYAML just for a four-field
header.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillMetadata:
    """Frontmatter fields of one SKILL.md.

    ``path`` is the SKILL.md file itself. ``root`` is its directory
    (useful for locating auxiliary files like examples/).

    ``raw`` preserves the parsed frontmatter as a dict; callers that
    need fields beyond the explicit attributes can reach through.
    """

    name: str
    description: str
    path: Path
    user_invocable: bool = False
    model: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent


class SkillParseError(ValueError):
    """Raised when a SKILL.md is missing frontmatter or key fields."""


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---\\n...\\n---\\n`` frontmatter from body.

    Returns ``(fields, body)``. Missing frontmatter raises.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillParseError("missing '---' frontmatter delimiters")

    fields: dict[str, Any] = {}
    current_list_key: str | None = None
    current_list: list[str] = []

    for line in match.group("frontmatter").splitlines():
        stripped = line.rstrip()
        if not stripped:
            # Blank line ends any list accumulation.
            if current_list_key is not None:
                fields[current_list_key] = current_list
                current_list_key = None
                current_list = []
            continue

        # List continuation: "  - item"
        list_item = re.match(r"^\s*-\s+(.+)$", stripped)
        if list_item is not None and current_list_key is not None:
            current_list.append(list_item.group(1).strip())
            continue

        # Finalize any list before starting a new key.
        if current_list_key is not None:
            fields[current_list_key] = current_list
            current_list_key = None
            current_list = []

        # Key: value
        kv = re.match(r"^([A-Za-z][A-Za-z0-9_\-]*)\s*:\s*(.*)$", stripped)
        if kv is None:
            # Silently skip malformed lines — Claude skills have been
            # seen with comments mid-frontmatter.
            continue
        key = kv.group(1).lower().replace("-", "_")
        value = kv.group(2).strip()
        if value == "":
            # Possibly a list header ("tags:\n  - a\n  - b")
            current_list_key = key
            current_list = []
        else:
            fields[key] = _coerce_value(value)

    if current_list_key is not None:
        fields[current_list_key] = current_list

    return fields, match.group("body")


def _coerce_value(value: str) -> bool | str:
    """Turn a raw frontmatter value into a bool / string / stripped-quote string."""
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    # Strip matching quotes.
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _require(fields: dict[str, Any], key: str, path: Path) -> str:
    v = fields.get(key)
    if not isinstance(v, str) or not v.strip():
        raise SkillParseError(
            f"{path}: frontmatter missing required field '{key}'"
        )
    return v.strip()


def parse_skill_metadata(path: Path) -> SkillMetadata:
    """Parse one SKILL.md file. Raises on missing name/description."""
    text = path.read_text(encoding="utf-8")
    fields, _body = _parse_frontmatter(text)

    name = _require(fields, "name", path)
    description = _require(fields, "description", path)
    user_invocable = bool(fields.get("user_invocable", False))
    model_value = fields.get("model", "")
    model = model_value if isinstance(model_value, str) else ""

    tags_raw = fields.get("tags", [])
    if isinstance(tags_raw, list):
        tags = tuple(str(t).strip() for t in tags_raw if str(t).strip())
    elif isinstance(tags_raw, str):
        tags = tuple(t.strip() for t in tags_raw.split(",") if t.strip())
    else:
        tags = ()

    return SkillMetadata(
        name=name,
        description=description,
        path=path,
        user_invocable=user_invocable,
        model=model,
        tags=tags,
        raw=fields,
    )


def iter_skill_metadata(root: Path) -> Iterator[SkillMetadata]:
    """Walk ``root``, yielding metadata for every SKILL.md found.

    Broken SKILL.md files are skipped with a warning log rather than
    aborting discovery — one typo in one skill shouldn't make all the
    others invisible.
    """
    if not root.is_dir():
        return
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            yield parse_skill_metadata(skill_md)
        except SkillParseError as exc:
            logger.warning("skills_loader: skipping %s (%s)", skill_md, exc)


@lru_cache(maxsize=64)
def load_skill_body(path: Path) -> str:
    """Return the markdown body (everything after the closing '---').

    Cached — a skill's body doesn't change during one engine run.
    Tests can clear via ``load_skill_body.cache_clear()``.
    """
    text = path.read_text(encoding="utf-8")
    _fields, body = _parse_frontmatter(text)
    return body.lstrip("\n")


__all__ = [
    "SkillMetadata",
    "SkillParseError",
    "iter_skill_metadata",
    "load_skill_body",
    "parse_skill_metadata",
]

"""Versioned prompt templates.

File convention (PROJECT_STRUCTURE.md): ``<stage>.<variant>.prompt.md`` with a
frontmatter header carrying version and input/output schemas. Frontmatter is a
deliberately tiny ``key: value`` format (JSON for structured values) so the
core stays stdlib-only; it is NOT full YAML and the parser below is strict
about what it accepts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FRONTMATTER_KEYS_REQUIRED = ("id", "stage", "variant", "version")
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class PromptFormatError(Exception):
    pass


class PromptRenderError(Exception):
    pass


@dataclass(frozen=True)
class PromptTemplate:
    id: str
    stage: str
    variant: str
    version: int
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    body: str
    source: str = "<memory>"

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.body))

    def render(self, variables: dict[str, Any]) -> str:
        """Fill ``{{name}}`` placeholders; every placeholder must be provided.

        Unknown extra variables are allowed (callers may pass a superset),
        but an unfilled placeholder is an error — silently shipping a literal
        ``{{var}}`` to a model is how prompts rot unnoticed.
        """
        missing = self.placeholders() - set(variables)
        if missing:
            raise PromptRenderError(
                f"prompt {self.id!r} missing variables: {', '.join(sorted(missing))}"
            )

        def substitute(match: re.Match[str]) -> str:
            value = variables[match.group(1)]
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

        return _PLACEHOLDER.sub(substitute, self.body)


def parse_prompt_text(text: str, source: str = "<memory>") -> PromptTemplate:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PromptFormatError(f"{source}: missing opening '---' frontmatter fence")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        raise PromptFormatError(f"{source}: missing closing '---' frontmatter fence")

    meta: dict[str, Any] = {}
    for raw_line in lines[1:end]:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise PromptFormatError(f"{source}: bad frontmatter line {raw_line!r}")
        key = key.strip()
        value = value.strip()
        if value.startswith("{") or value.startswith("["):
            try:
                meta[key] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise PromptFormatError(f"{source}: invalid JSON for {key!r}: {exc}")
        else:
            meta[key] = value

    for required in _FRONTMATTER_KEYS_REQUIRED:
        if required not in meta:
            raise PromptFormatError(f"{source}: frontmatter missing {required!r}")

    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise PromptFormatError(f"{source}: empty prompt body")

    return PromptTemplate(
        id=str(meta["id"]),
        stage=str(meta["stage"]),
        variant=str(meta["variant"]),
        version=int(meta["version"]),
        input_schema=dict(meta.get("input_schema") or {}),
        output_schema=dict(meta.get("output_schema") or {}),
        body=body,
        source=source,
    )


class PromptRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}

    def add(self, template: PromptTemplate) -> None:
        if template.id in self._templates:
            raise ValueError(f"prompt {template.id!r} already registered")
        self._templates[template.id] = template

    def get(self, prompt_id: str) -> PromptTemplate:
        template = self._templates.get(prompt_id)
        if template is None:
            raise KeyError(f"prompt {prompt_id!r} not found")
        return template

    def ids(self) -> list[str]:
        return sorted(self._templates)

    @staticmethod
    def load_dir(directory: str | Path) -> "PromptRegistry":
        registry = PromptRegistry()
        directory = Path(directory)
        for path in sorted(directory.glob("*.prompt.md")):
            registry.add(parse_prompt_text(path.read_text(encoding="utf-8"), str(path)))
        return registry


#: agents/prompts relative to this file (agents/skills/src/omm_agent_skills/..)
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def load_default_registry() -> PromptRegistry:
    return PromptRegistry.load_dir(DEFAULT_PROMPTS_DIR)

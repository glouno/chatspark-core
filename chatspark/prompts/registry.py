from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


@dataclass(frozen=True)
class PromptTemplate:
    prompt_id: str
    version: str
    relative_path: str
    content: str
    required_variables: tuple[str, ...]
    sha256: str
    frontmatter: dict[str, Any]

    def render(self, **variables: Any) -> str:
        missing = [name for name in self.required_variables if name not in variables]
        if missing:
            raise KeyError(f"prompt {self.prompt_id} missing variables: {', '.join(missing)}")
        return string.Template(self.content).substitute(
            {key: str(value) for key, value in variables.items()}
        )

    def metadata(self) -> dict[str, Any]:
        extra = {
            key: value
            for key, value in self.frontmatter.items()
            if key not in {"prompt_id", "version", "required_variables"}
        }
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "sha256": self.sha256,
            "path": f"chatspark/prompts/{self.relative_path}",
            "required_variables": list(self.required_variables),
            **extra,
        }


PROMPT_PATHS = {
    "rag.classic_chat.system.v6": "rag/classic_chat/system_v6.md",
    "rag.classic_chat.user.v2": "rag/classic_chat/user_v2.md",
}


def load_prompt(prompt_id: str) -> PromptTemplate:
    try:
        relative_path = PROMPT_PATHS[prompt_id]
    except KeyError as exc:
        raise KeyError(f"unknown prompt id: {prompt_id}") from exc
    prompt_path = files("chatspark.prompts").joinpath(relative_path)
    raw = prompt_path.read_text(encoding="utf-8")
    metadata, content = _split_frontmatter(raw)
    declared_id = str(metadata.get("prompt_id") or "").strip()
    if declared_id != prompt_id:
        raise ValueError(f"{relative_path}: prompt_id {declared_id!r} does not match {prompt_id!r}")
    required = tuple(
        str(item).strip() for item in metadata.get("required_variables", []) if str(item).strip()
    )
    return PromptTemplate(
        prompt_id=prompt_id,
        version=str(metadata.get("version") or "1"),
        relative_path=relative_path,
        content=content.strip(),
        required_variables=required,
        sha256=hashlib.sha256(content.strip().encode("utf-8")).hexdigest(),
        frontmatter=metadata,
    )


def render_prompt(prompt_id: str, **variables: Any) -> str:
    return load_prompt(prompt_id).render(**variables)


def prompt_trace(*prompt_ids: str) -> dict[str, Any]:
    prompts = [load_prompt(prompt_id) for prompt_id in prompt_ids]
    combined = "\n".join(prompt.sha256 for prompt in prompts)
    return {
        "messages": [prompt.metadata() for prompt in prompts],
        "combined_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
    }


def iter_prompt_templates() -> list[PromptTemplate]:
    return [load_prompt(prompt_id) for prompt_id in sorted(PROMPT_PATHS)]


def prompt_inventory() -> list[dict[str, Any]]:
    return [prompt.metadata() for prompt in iter_prompt_templates()]


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    try:
        header, content = raw[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("prompt frontmatter is not closed") from exc
    return _parse_simple_frontmatter(header), content


def _parse_simple_frontmatter(header: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in header.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if current_list_key and stripped.startswith("- "):
            metadata.setdefault(current_list_key, []).append(stripped[2:].strip())
            continue
        current_list_key = None
        if ":" not in stripped:
            raise ValueError(f"invalid prompt frontmatter line: {raw_line!r}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            metadata[key] = []
            current_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            metadata[key] = [item.strip() for item in inner.split(",") if item.strip()]
        else:
            metadata[key] = value.strip("\"'")
    return metadata

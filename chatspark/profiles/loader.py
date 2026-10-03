import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path

import yaml

from chatspark.profiles.models import Profile


class ProfileError(ValueError):
    pass


def _profile_path(name):
    candidate = Path(name).expanduser()
    if candidate.exists():
        return candidate / "profile.yaml" if candidate.is_dir() else candidate
    roots = [files("chatspark.profiles").joinpath("bundled")]
    roots += [
        Path(p).expanduser() for p in os.getenv("CHATSPARK_PROFILE_PATH", "").split(os.pathsep) if p
    ]
    for root in roots:
        candidate = root / name / "profile.yaml"
        if candidate.is_file():
            return Path(str(candidate))
    raise ProfileError(f"Unknown profile {name!r}")


def available_profiles():
    roots = [files("chatspark.profiles").joinpath("bundled")]
    roots += [Path(p) for p in os.getenv("CHATSPARK_PROFILE_PATH", "").split(os.pathsep) if p]
    return sorted(
        {
            p.name
            for root in roots
            if root.is_dir()
            for p in root.iterdir()
            if p.is_dir() and (p / "profile.yaml").is_file()
        }
    )


def _merge(base, overlay):
    result = dict(base)
    for key, value in overlay.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


def _resolve_prompt_paths(data, directory):
    generation = data.get("generation")
    if not isinstance(generation, dict):
        return
    for field in ("system_prompt", "user_prompt"):
        value = generation.get(field)
        if isinstance(value, str) and value:
            target = Path(value).expanduser()
            generation[field] = str(
                (target if target.is_absolute() else directory / target).resolve()
            )


def _resolve_provider_paths(data, directory):
    from chatspark.runtime.registry import get_registry

    registry = get_registry()

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            provider_id = value.get("provider")
            descriptor = (
                registry.providers.get(provider_id) if isinstance(provider_id, str) else None
            )
            if descriptor and isinstance(value.get("settings"), dict):
                for field in descriptor.reference_fields:
                    owner = value["settings"]
                    components = field.split(".")
                    for component in components[:-1]:
                        owner = owner.get(component) if isinstance(owner, dict) else None
                    if isinstance(owner, dict) and owner.get(components[-1]):
                        reference = owner[components[-1]]
                        if not isinstance(reference, str):
                            raise ProfileError("Provider file reference must be a string")
                        target = Path(reference).expanduser()
                        owner[components[-1]] = str(
                            (target if target.is_absolute() else directory / target).resolve()
                        )
            for child in value.values():
                visit(child)

    visit(data)


def resolve_profile_data(path, stack=()):
    path = _profile_path(str(path)).resolve()
    if path in stack:
        raise ProfileError("Profile inheritance cycle")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ProfileError("Profile must be a mapping")
    result = {}
    sources = []
    parents = data.pop("extends", [])
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, list) or any(not isinstance(p, str) for p in parents):
        raise ProfileError("extends must contain profile references")
    for reference in parents:
        target = path.parent / reference
        parent, inherited = resolve_profile_data(
            target if target.exists() else reference, (*stack, path)
        )
        result = _merge(result, parent)
        sources.extend(inherited)
    sections = data.pop("sections", {})
    if not isinstance(sections, dict) or any(
        not isinstance(key, str) or not isinstance(reference, str)
        for key, reference in sections.items()
    ):
        raise ProfileError("sections must map section names to files")
    _resolve_prompt_paths(data, path.parent)
    _resolve_provider_paths(data, path.parent)
    result = _merge(result, data)
    for key, reference in sections.items():
        section_path = (path.parent / reference).resolve()
        section = yaml.safe_load(section_path.read_text())
        if not isinstance(section, dict):
            raise ProfileError("External section must be a mapping")
        section.pop("section_version", None)
        values = section.get(key, section)
        if not isinstance(values, dict):
            raise ProfileError("External section must contain a mapping")
        _resolve_prompt_paths({key: values}, section_path.parent)
        _resolve_provider_paths(values, section_path.parent)
        result[key] = _merge(result.get(key, {}), values)
    return result, [*sources, str(path)]


def load_profile(name="hybrid"):
    path = _profile_path(name)
    data, _ = resolve_profile_data(path)
    if data.get("profile_version") != 2:
        raise ProfileError(
            "Normal execution requires explicit profile_version: 2. "
            "Convert old profiles once using the private maintenance tool."
        )
    profile = Profile.model_validate(data)
    for field in ("system_prompt", "user_prompt"):
        value = getattr(profile.generation, field)
        if value:
            target = Path(value).expanduser()
            target = target if target.is_absolute() else path.parent / target
            if not target.is_file():
                raise ProfileError(f"Missing explicit {field} file")
            setattr(profile.generation, field, str(target.resolve()))
    return profile


def profile_digest(profile):
    return hashlib.sha256(
        json.dumps(profile.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def resolved_profile_artifact(name):
    profile = load_profile(str(name))
    return {
        "resolved_profile_version": 2,
        "profile": profile.model_dump(mode="json"),
        "profile_digest": profile_digest(profile),
    }

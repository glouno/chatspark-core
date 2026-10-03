"""Compose provider selections from installed, operator-approved models."""

import copy

from chatspark.profiles.models import Profile


def composed_profile_schema(registry):
    schema = copy.deepcopy(Profile.model_json_schema())
    definitions = schema.setdefault("$defs", {})
    capabilities = {}
    for capability in {c for p in registry.providers.values() for c in p.capabilities}:
        variants = []
        for provider in sorted(registry.providers.values(), key=lambda p: p.provider_id):
            if capability not in provider.capabilities:
                continue
            key = provider.provider_id.replace(".", "_") + "_" + capability.replace("-", "_")
            options = copy.deepcopy(provider.settings_model.model_json_schema())
            local = options.pop("$defs", {})
            renamed = {name: f"{key}_{name}" for name in local}

            def rewrite(value):
                if isinstance(value, dict):
                    for field, item in value.items():
                        if field == "$ref" and item.startswith("#/$defs/"):
                            name = item.removeprefix("#/$defs/")
                            value[field] = "#/$defs/" + renamed.get(name, name)
                        else:
                            rewrite(item)
                elif isinstance(value, list):
                    for item in value:
                        rewrite(item)

            rewrite(options)
            for name, value in local.items():
                rewrite(value)
                definitions[renamed[name]] = value
            definitions[key] = options
            variants.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["provider"],
                    "properties": {
                        "provider": {"const": provider.provider_id},
                        "settings": {"$ref": "#/$defs/" + key},
                    },
                }
            )
        capabilities[capability] = {"oneOf": variants}

    # Hybrid's nested selections must use the same installed/approved schemas
    # as outer selections. Preserve lane instance names, limits and weights.
    hybrid = definitions.get("hybrid_strategy", {}).get("properties", {})
    if "lanes" in hybrid:
        lanes = hybrid["lanes"]
        item_ref = lanes["items"]["$ref"].removeprefix("#/$defs/")
        instance = definitions[item_ref]
        lane_variants = []
        for selection in capabilities.get("retrieval", {}).get("oneOf", []):
            lane = copy.deepcopy(instance)
            lane["properties"].update(copy.deepcopy(selection["properties"]))
            lane_variants.append(lane)
        lanes["items"] = {"oneOf": lane_variants} if lane_variants else {"not": {}}
    for field, capability in (("planner", "planner"), ("processor", "processor")):
        if field in hybrid:
            hybrid[field] = {"anyOf": [capabilities.get(capability, {"not": {}}), {"type": "null"}]}
    schema["properties"]["chunking"] = capabilities["chunker"]
    retrieval = definitions["RetrievalProfile"]["properties"]
    retrieval["strategy"] = capabilities["strategy"]
    retrieval["context"] = capabilities["context"]
    definitions["ExtractionProfile"]["properties"]["html"] = capabilities["html"]
    for field, capability in [("provider", "pdf"), ("inspector", "pdf-inspector")]:
        definitions["PdfProfile"]["properties"][field] = (
            {"anyOf": [capabilities[capability], {"type": "null"}]}
            if field == "inspector" and capability in capabilities
            else capabilities.get(capability, {"type": "null"})
        )
    definitions["PdfProfile"]["properties"]["fallbacks"] = {
        "type": "array",
        "items": capabilities["pdf"],
    }
    for section, field, capability in [
        ("ExtractionProfile", "ocr", "ocr"),
        ("GenerationProfile", "remote_prompt", "prompt"),
    ]:
        definitions[section]["properties"][field] = {
            "anyOf": [capabilities.get(capability, {"not": {}}), {"type": "null"}]
        }
    schema["properties"]["policy"] = {
        "anyOf": [capabilities.get("policy", {"not": {}}), {"type": "null"}]
    }
    schema["properties"]["artifact_builders"] = {
        "type": "array",
        "items": capabilities.get("artifact", {"not": {}}),
    }
    schema["x-plugin-api-version"] = 2
    return schema

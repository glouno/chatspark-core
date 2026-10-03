import json
from typing import Any


def normalize_heading_path(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except json.JSONDecodeError:
                pass
        if " > " in text:
            return [part.strip() for part in text.split(" > ") if part.strip()]
        return [text] if text else []
    return [str(value)]

import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chatspark.plugins.contracts import RankedEvidence
from chatspark.prompts import load_prompt
from chatspark.storage.evidence import EvidenceStore


def resolve_messages(question, chunks, profile, *, context=None):
    if context is not None:
        chunks = EvidenceStore(context.database).resolve(
            [RankedEvidence(c.chunk_id, c.score) for c in chunks], context
        )
    policy = profile.generation
    remote_provenance = {}
    system_provider = "local" if policy.system_prompt else "bundled"
    system = (
        Path(policy.system_prompt).read_text()
        if policy.system_prompt
        else load_prompt("rag.classic_chat.system.v6").content
    )
    user = (
        Path(policy.user_prompt).read_text()
        if policy.user_prompt
        else load_prompt("rag.classic_chat.user.v2").content
    )
    if not policy.system_prompt and policy.remote_prompt:
        from chatspark.plugins.contracts import ResolvedPrompt
        from chatspark.runtime.registry import get_registry

        try:
            resolved = get_registry().resolve(policy.remote_prompt, "prompt").get_system_prompt()
            if not isinstance(resolved, ResolvedPrompt) or not resolved.text.strip():
                raise ValueError("Invalid remote prompt")
            system = resolved.text
            remote_provenance = {
                "system_revision": resolved.revision,
                "system_implementation_version": resolved.implementation_version,
            }
            system_provider = policy.remote_prompt.provider
        except Exception:
            if not policy.allow_remote_fallback:
                raise RuntimeError("Remote prompt provider failed") from None
            system_provider = "bundled_fallback"
    system += "\nReference date: " + datetime.now(ZoneInfo(policy.timezone)).date().isoformat()
    excerpts = []
    remaining = policy.max_context_chars
    for i, chunk in enumerate(chunks, 1):
        value = f"[C{i}] {chunk.metadata.get('title') or 'Source'}\n{chunk.content}\n"
        excerpts.append(value[:remaining])
        remaining -= min(remaining, len(value))
        if remaining <= 0:
            break
    from string import Template

    rendered = Template(user).substitute(question=question, context="\n".join(excerpts))
    return [{"role": "system", "content": system}, {"role": "user", "content": rendered}], {
        "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "user_sha256": hashlib.sha256(user.encode()).hexdigest(),
        "included_evidence_ids": [c.chunk_id for c in chunks[: len(excerpts)]],
        "system_provider": system_provider,
        "user_provider": "local" if policy.user_prompt else "bundled",
        **remote_provenance,
    }

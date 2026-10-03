---
prompt_id: rag.classic_chat.user.v2
version: 2
origin: chatspark/generation/prompts.py:build_rag_messages
used_by:
  - chatspark.generation.prompts.build_rag_messages
  - scripts/eval/run_fixed_context_llm_eval.py
required_variables:
  - question
  - context
---
Question:
${question}

Excerpts:
${context}

"""Explicit local sentence-transformer binding; no remote model code/downloads."""

from pathlib import Path

from chatspark.embeddings.formatting import format_embedding_texts


class LocalEmbedder:
    def __init__(
        self, model_name, *, device=None, normalize=True, text_format="auto", batch_size=64
    ):
        from sentence_transformers import SentenceTransformer

        if not Path(model_name).is_dir():
            raise ValueError("Prepare a local embedding model first and select its directory")
        self.model_name = model_name
        self.normalize = normalize
        self.text_format = text_format
        self.batch_size = batch_size
        self.model = SentenceTransformer(
            model_name, device=device, trust_remote_code=False, local_files_only=True
        )
        self.dimensions = self.model.get_sentence_embedding_dimension()

    def embed_texts(self, texts):
        return self.model.encode(
            list(texts), batch_size=self.batch_size, normalize_embeddings=self.normalize
        ).tolist()

    def _embed_role(self, texts, role):
        return self.embed_texts(
            format_embedding_texts(
                texts, role=role, model_name=self.model_name, configured_format=self.text_format
            )
        )

    def embed_document_texts(self, texts):
        return self._embed_role(texts, "passage")

    def embed_query_texts(self, texts):
        return self._embed_role(texts, "query")

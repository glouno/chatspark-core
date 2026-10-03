import requests

from chatspark.embeddings.base import Embedder
from chatspark.embeddings.formatting import format_embedding_texts
from chatspark.runtime.config import settings


class OpenAICompatibleEmbedder(Embedder):
    def __init__(
        self,
        base_url: str | None = None,
        model_name: str | None = None,
        api_key: str | None = None,
        text_format: str | None = None,
        normalize: bool | None = None,
    ):
        self.text_format = (
            text_format if text_format is not None else settings.EMBEDDING_TEXT_FORMAT
        )
        self.normalize = settings.EMBEDDING_NORMALIZE if normalize is None else normalize
        self._base_url = base_url or settings.OPENAI_COMPAT_BASE_URL
        self._model_name = model_name or settings.EMBEDDING_MODEL
        self._api_key = api_key if api_key is not None else settings.OPENAI_COMPAT_API_KEY
        self.dimensions = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embed(
            format_embedding_texts(
                texts,
                role="query",
                model_name=self._model_name,
                configured_format=self.text_format,
            )
        )

    def embed_document_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embed(
            format_embedding_texts(
                texts,
                role="passage",
                model_name=self._model_name,
                configured_format=self.text_format,
            )
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        response = requests.post(
            f"{self._base_url}/embeddings",
            headers=headers,
            json={"model": self._model_name, "input": texts},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        import math

        records = sorted(data["data"], key=lambda item: item["index"])
        if [item["index"] for item in records] != list(range(len(texts))):
            raise ValueError("Embedding endpoint returned inconsistent indexes")
        embeddings = [item["embedding"] for item in records]
        if not embeddings or any(
            not vector or not all(math.isfinite(v) for v in vector) for vector in embeddings
        ):
            raise ValueError("Embedding endpoint returned invalid vectors")
        if len({len(vector) for vector in embeddings}) != 1:
            raise ValueError("Embedding endpoint returned inconsistent dimensions")
        dimensions = len(embeddings[0])
        if self.dimensions is not None and dimensions != self.dimensions:
            raise ValueError("Embedding endpoint changed dimensions")
        self.dimensions = dimensions
        if self.normalize:
            embeddings = [
                [v / (math.sqrt(sum(x * x for x in vector)) or 1) for v in vector]
                for vector in embeddings
            ]
        return embeddings

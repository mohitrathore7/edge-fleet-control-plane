"""Small dependency-free vector store for log + runbook retrieval.

Embeddings are L2-normalised hashed bag-of-n-grams (unigrams, bigrams and error
codes), which works offline and on CPU-only gateways. `Embedder` is an interface:
swap in sentence-transformers or an embeddings API without touching retrieval.
"""
from __future__ import annotations

import hashlib
import itertools
import re
import threading
from dataclasses import dataclass, field

import numpy as np

TOKEN = re.compile(r"[a-z0-9_.\-]+")
STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "is", "are", "was", "why", "what", "did",
        "does", "with", "my", "this", "that", "it", "be", "do", "how", "after", "from", "at"}


class HashingEmbedder:
    def __init__(self, dim: int = 2048) -> None:
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        toks = [t.strip(".-") for t in TOKEN.findall(text.lower())]
        toks = [t for t in toks if t and t not in STOP]
        return toks + [f"{a}_{b}" for a, b in itertools.pairwise(toks)]

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for f in self._features(text):
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0  # signed hashing reduces collision bias
        n = np.linalg.norm(v)
        return v / n if n else v


@dataclass
class Doc:
    doc_id: str
    text: str
    meta: dict = field(default_factory=dict)


class VectorStore:
    def __init__(self, embedder=None, capacity: int = 50_000) -> None:
        self.emb = embedder or HashingEmbedder()
        self.capacity = capacity
        self.docs: list[Doc] = []
        self._vecs = np.zeros((0, self.emb.dim), dtype=np.float32)
        self._lock = threading.Lock()

    def add(self, doc: Doc) -> None:
        v = self.emb.embed(doc.text)[None, :]
        with self._lock:
            self.docs.append(doc)
            self._vecs = np.vstack([self._vecs, v])
            if len(self.docs) > self.capacity:  # ring-buffer behaviour for log streams
                drop = len(self.docs) - self.capacity
                self.docs, self._vecs = self.docs[drop:], self._vecs[drop:]

    def search(self, query: str, k: int = 5, where: dict | None = None) -> list[tuple[float, Doc]]:
        q = self.emb.embed(query)
        with self._lock:
            if not self.docs:
                return []
            scores = self._vecs @ q
            docs = self.docs
        idx = np.argsort(-scores)
        out = []
        for i in idx:
            d = docs[i]
            if where and any(d.meta.get(key) != val for key, val in where.items()):
                continue
            out.append((float(scores[i]), d))
            if len(out) == k:
                break
        return out

    def __len__(self) -> int:
        return len(self.docs)

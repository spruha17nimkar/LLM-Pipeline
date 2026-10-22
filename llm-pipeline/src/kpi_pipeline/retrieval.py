"""BM25 retrieval over filing chunks (dependency-free).

Lexical retrieval works well here: KPI line items use stable vocabulary
("Net cash provided by operating activities"), and it's fully deterministic,
which keeps evals reproducible. A small boost favours the financial statements
and MD&A sections, plus chunks that actually contain numeric tables.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from .models import Chunk

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "of", "and", "a", "an", "in", "to", "for", "by", "on", "at", "or", "as", "is"}
_PREFERRED = re.compile(r"financial statements|management.s discussion|selected financial", re.I)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.tfs = [Counter(tokenize(c.text)) for c in chunks]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avgdl = (sum(self.lens) / len(self.lens)) if chunks else 0.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def score(self, query: str) -> list[float]:
        q = tokenize(query)
        scores = []
        for tf, dl, chunk in zip(self.tfs, self.lens, self.chunks):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            if s:
                if _PREFERRED.search(chunk.section):
                    s *= 1.25
                if chunk.text.count("|") >= 8:  # looks like a financial table
                    s *= 1.15
            scores.append(s)
        return scores

    def search(self, query: str, k: int = 6) -> list[Chunk]:
        scored = sorted(zip(self.score(query), range(len(self.chunks))), key=lambda x: (-x[0], x[1]))
        return [self.chunks[i] for s, i in scored[:k] if s > 0]

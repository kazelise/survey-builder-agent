"""Pure-Python Okapi BM25 -- zero third-party dependencies, so it's the
"always works" leg of `search_handbook`'s retrieval (DESIGN.md's "no
framework" pillar extends here: no `rank_bm25`, no `numpy`). Costs
microseconds to build/query over a corpus this small (~a few hundred
chunks), so there's no need to persist the inverted index -- it's rebuilt
in memory from `data/handbook_index.json`'s chunk text every time the
process starts (see rag/index.py).

Tokenizer note: the handbook corpus is bilingual (en + zh-CN). ASCII/digit
runs become lowercased word tokens. Chinese has no whitespace between
words and this project intentionally has zero NLP dependencies (no
jieba/spaCy), so CJK runs are tokenized as character bigrams -- a
well-known cheap approximation for Chinese BM25 without a real segmenter:
it captures most 2+ character terms (the vast majority of Chinese words)
while staying a five-line regex.
"""

from __future__ import annotations

import math
import re

# CJK Unified Ideographs, Extension A, and Compatibility Ideographs --
# spelled out as \uXXXX escapes (not literal characters) so the source
# file stays plain ASCII and diff/grep-safe.
_CJK_RANGES = "一-鿿㐀-䶿豈-﫿"
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[" + _CJK_RANGES + "]+")

# BM25's own smoothing constants (Robertson-Sparck Jones), not chunker
# knobs -- standard defaults, not exposed as config since there's no
# evidence yet that tuning them would matter for a corpus this size.
K1 = 1.5
B = 0.75


def _cjk_bigrams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        chunk = m.group(0)
        if chunk[0].isascii():
            tokens.append(chunk)
        else:
            tokens.extend(_cjk_bigrams(chunk))
    return tokens


class BM25Index:
    """Build once over a list of document texts; `.search(query, top_k)`
    returns `[(doc_idx, score), ...]` sorted by descending score, scores
    strictly greater than zero only."""

    def __init__(self, texts: list[str], *, k1: float = K1, b: float = B):
        self.k1 = k1
        self.b = b
        docs = [tokenize(t) for t in texts]
        self._n = len(docs)
        self._doc_lens = [len(d) for d in docs]
        self._avgdl = (sum(self._doc_lens) / self._n) if self._n else 0.0

        self._term_freqs: list[dict[str, int]] = []
        self._doc_freq: dict[str, int] = {}
        for doc in docs:
            counts: dict[str, int] = {}
            for tok in doc:
                counts[tok] = counts.get(tok, 0) + 1
            self._term_freqs.append(counts)
            for tok in counts:
                self._doc_freq[tok] = self._doc_freq.get(tok, 0) + 1

    def _idf(self, term: str) -> float:
        n_qi = self._doc_freq.get(term, 0)
        # +1 inside the log keeps IDF non-negative even for terms in every
        # document (the BM25+ / Lucene-style variant of Robertson's IDF).
        return math.log((self._n - n_qi + 0.5) / (n_qi + 0.5) + 1)

    def score(self, query: str, doc_idx: int) -> float:
        tf = self._term_freqs[doc_idx]
        dl = self._doc_lens[doc_idx]
        total = 0.0
        for term in tokenize(query):
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = self._idf(term)
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1.0))
            total += idf * (f * (self.k1 + 1)) / denom
        return total

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        if self._n == 0:
            return []
        scored = [(i, self.score(query, i)) for i in range(self._n)]
        scored = [pair for pair in scored if pair[1] > 0.0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]

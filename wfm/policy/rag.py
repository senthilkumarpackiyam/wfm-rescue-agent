"""Policy RAG: hybrid search (Chroma embeddings + BM25 keywords) over data/policy/*.md.

Each "## RULE-ID Title" section is one retrievable chunk, so answers cite rule IDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
from rank_bm25 import BM25Okapi

from wfm.config import DATA_DIR

POLICY_DIR = DATA_DIR / "policy"
RRF_K = 60  # reciprocal-rank-fusion constant
_STOP = set("a an and are as at be by can for from how i if in is it may must no not of on or "
            "the their this to we what when which who with".split())


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    title: str
    text: str
    document: str  # e.g. "Overtime (WFM-POL-02)"


def load_rules() -> list[PolicyRule]:
    rules = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        doc_title = ""
        for block in re.split(r"(?m)^## ", path.read_text()):
            if block.startswith("# "):
                doc_title = block.splitlines()[0][2:].strip()
                continue
            header, _, body = block.partition("\n")
            rule_id, _, title = header.strip().partition(" ")
            rules.append(PolicyRule(rule_id, title.strip(), " ".join(body.split()), doc_title))
    return rules


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]


class PolicyIndex:
    def __init__(self) -> None:
        self.rules = load_rules()
        self.by_id = {r.rule_id: r for r in self.rules}
        docs = [f"{r.rule_id} {r.title}. {r.text}" for r in self.rules]
        client = chromadb.EphemeralClient(Settings(anonymized_telemetry=False))
        self.collection = client.get_or_create_collection(
            "wfm_policy", embedding_function=embedding_functions.DefaultEmbeddingFunction())
        self.collection.upsert(ids=[r.rule_id for r in self.rules], documents=docs)
        self.bm25 = BM25Okapi([_tokens(d) for d in docs])

    def search(self, query: str, k: int = 4) -> list[dict]:
        n = len(self.rules)
        dense = self.collection.query(query_texts=[query], n_results=min(10, n))["ids"][0]
        scores = self.bm25.get_scores(_tokens(query))
        sparse = [self.rules[i].rule_id for i in sorted(range(n), key=lambda i: -scores[i])[:10]]
        fused: dict[str, float] = {}
        for ranking in (dense, sparse):
            for rank, rule_id in enumerate(ranking):
                fused[rule_id] = fused.get(rule_id, 0.0) + 1 / (RRF_K + rank + 1)
        top = sorted(fused, key=lambda rid: -fused[rid])[:k]
        return [{"rule_id": rid, "title": self.by_id[rid].title, "document": self.by_id[rid].document,
                 "text": self.by_id[rid].text} for rid in top]


@lru_cache
def policy_index() -> PolicyIndex:
    return PolicyIndex()


def search_policy(query: str, k: int = 4) -> list[dict]:
    return policy_index().search(query, k)


def get_rule(rule_id: str) -> PolicyRule | None:
    return policy_index().by_id.get(rule_id)

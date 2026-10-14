"""Keyword (BM25) search over work orders and the maintenance manual.

Why BM25 and not embeddings (for now):
  * Shop-floor notes are full of exact tokens that matter: alarm codes
    (CL-302), part numbers (APKT1604), batch IDs (TM-2607). Lexical match
    handles these better than a general-purpose embedding model.
  * Notes are mixed Chinese/English. The tokenizer indexes CJK text as
    character unigrams + bigrams, so 濾網堵塞 matches 濾網 without a
    Chinese word segmenter.
  * Zero infra and zero dependencies: the index builds in milliseconds.
A hybrid (BM25 + embeddings) retriever is the obvious next step once the
eval shows semantic misses (e.g. "overheating" vs "temp rise").
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import config

_TOKEN = re.compile(r"[a-z0-9]+(?:[-_./][a-z0-9]+)*|[\u4e00-\u9fff]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "was", "with", "at", "by", "it",
         "this", "that", "be", "as", "from", "are", "why", "what", "how", "did", "does", "do"}


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for m in _TOKEN.finditer(text.lower()):
        t = m.group(0)
        if "\u4e00" <= t[0] <= "\u9fff":
            tokens.extend(t)
            tokens.extend(t[i:i + 2] for i in range(len(t) - 1))
        elif t not in _STOP:
            tokens.append(t)
            if "-" in t:  # "cl-302" also matches "302"; "tm-2607" also matches "tm"
                tokens.extend(p for p in t.split("-") if p and p not in _STOP)
    return tokens


@dataclass
class Doc:
    doc_id: str
    source: str  # "work_orders" | "manual"
    title: str
    text: str
    machine_id: str | None = None
    date: str | None = None  # YYYY-MM-DD


class BM25Index:
    def __init__(self, docs: list[Doc], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.tfs = [Counter(tokenize(f"{d.title} {d.text}")) for d in docs]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg_len = (sum(self.lens) / len(self.lens)) if docs else 0.0
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, top_k: int = 5, source: str | None = None, machine_id: str | None = None,
               date_from: str | None = None, date_to: str | None = None) -> list[tuple[Doc, float]]:
        q_terms = set(tokenize(query))
        scored = []
        for i, d in enumerate(self.docs):
            if source and source != "all" and d.source != source:
                continue
            if machine_id and d.source == "work_orders" and d.machine_id != machine_id:
                continue
            if d.date and ((date_from and d.date < date_from) or (date_to and d.date > date_to)):
                continue
            tf, dl, s = self.tfs[i], self.lens[i], 0.0
            for t in q_terms:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg_len))
            if s > 0:
                scored.append((d, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


def load_documents(db_path: Path = config.DB_PATH, manual_dir: Path = config.MANUAL_DIR) -> list[Doc]:
    docs: list[Doc] = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    for wo_id, mid, created, wo_type, comp, tech, notes in con.execute(
            "SELECT wo_id, machine_id, created_ts, wo_type, component, technician, notes FROM work_orders"):
        docs.append(Doc(doc_id=wo_id, source="work_orders", machine_id=mid, date=created[:10],
                        title=f"{wo_type} {mid} {comp}", text=f"{notes} (technician: {tech})"))
    con.close()
    for path in sorted(Path(manual_dir).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        doc_title = text.splitlines()[0].lstrip("# ").strip() if text else path.stem
        for section in re.split(r"\n(?=## )", text):
            lines = section.strip().splitlines()
            if not lines or not lines[0].startswith("## "):
                continue
            heading = lines[0][3:].strip()
            slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:40]
            docs.append(Doc(doc_id=f"MAN:{path.stem}#{slug}", source="manual",
                            title=f"{doc_title} / {heading}", text="\n".join(lines[1:]).strip()))
    return docs

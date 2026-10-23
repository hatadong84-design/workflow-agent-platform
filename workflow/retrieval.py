from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DOCS_DIR = Path(__file__).resolve().parent / "sample_docs"
MAX_CHUNK_CHARS = 900
MAX_EXCERPT_CHARS = 900
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+")


@dataclass(frozen=True)
class Chunk:
    source_id: str
    title: str
    path: Path
    text: str


def _chunks(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    result: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > MAX_CHUNK_CHARS:
            if current:
                result.append(current)
                current = ""
            result.append(paragraph[:MAX_CHUNK_CHARS])
            paragraph = paragraph[MAX_CHUNK_CHARS:]
        if current and len(current) + len(paragraph) + 1 > MAX_CHUNK_CHARS:
            result.append(current)
            current = paragraph
        else:
            current = f"{current}\n{paragraph}".strip()
    if current:
        result.append(current)
    return result


def tokenize(text: str) -> list[str]:
    """Small deterministic tokenizer: Chinese bigrams plus English/number terms."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        term = match.group(0)
        if term[0] >= "\u3400":
            if len(term) == 1:
                tokens.append(term)
            else:
                tokens.extend(term[index : index + 2] for index in range(len(term) - 1))
        else:
            tokens.append(term)
    return tokens


def _safe_doc_path(filename: str) -> Path:
    root = DOCS_DIR.resolve()
    candidate = (root / filename).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("文档路径超出样例资料目录")
    if candidate.suffix.lower() not in {".md", ".txt"} or not candidate.is_file():
        raise ValueError("只允许读取样例资料目录中的 Markdown 或 TXT 文件")
    return candidate


def load_corpus() -> list[Chunk]:
    root = DOCS_DIR.resolve()
    if not root.exists():
        return []
    chunks: list[Chunk] = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in {".md", ".txt"} or not path.is_file():
            continue
        safe_path = _safe_doc_path(path.name)
        content = safe_path.read_text(encoding="utf-8")
        for index, text in enumerate(_chunks(content)):
            chunks.append(
                Chunk(
                    source_id=f"{safe_path.stem}#{index}",
                    title=safe_path.stem,
                    path=safe_path,
                    text=text[:MAX_EXCERPT_CHARS],
                )
            )
    return chunks


def search_documents(query: str, top_k: int = 4) -> dict:
    query_tokens = tokenize(query)
    if not query_tokens:
        return {"matches": []}
    corpus = load_corpus()
    tokenized = [tokenize(chunk.text) for chunk in corpus]
    if not corpus:
        return {"matches": []}

    document_frequency: dict[str, int] = {}
    for terms in tokenized:
        for term in set(terms):
            document_frequency[term] = document_frequency.get(term, 0) + 1
    average_length = sum(map(len, tokenized)) / len(tokenized) or 1
    scored: list[tuple[float, Chunk]] = []
    for chunk, terms in zip(corpus, tokenized):
        frequencies: dict[str, int] = {}
        for term in terms:
            frequencies[term] = frequencies.get(term, 0) + 1
        score = 0.0
        length = len(terms)
        for term in query_tokens:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1 + (len(corpus) - document_frequency.get(term, 0) + 0.5) /
                           (document_frequency.get(term, 0) + 0.5))
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * length / average_length)
            score += idf * frequency * (1.2 + 1) / denominator
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda pair: (-pair[0], pair[1].source_id))
    matches = [
        {
            "source_id": chunk.source_id,
            "title": chunk.title,
            "excerpt": chunk.text,
            "score": round(score, 4),
        }
        for score, chunk in scored[: max(1, min(top_k, 8))]
    ]
    return {"matches": matches}


def read_document(source_id: str) -> dict:
    if "#" not in source_id:
        raise ValueError("source_id 格式无效")
    stem, raw_index = source_id.rsplit("#", 1)
    if not stem or not raw_index.isdigit():
        raise ValueError("source_id 格式无效")
    matches = [chunk for chunk in load_corpus() if chunk.source_id == source_id]
    if not matches:
        raise ValueError("找不到该来源，或来源不在样例资料目录中")
    chunk = matches[0]
    return {
        "source_id": chunk.source_id,
        "title": chunk.title,
        "excerpt": chunk.text[:MAX_EXCERPT_CHARS],
    }


def source_excerpts(results: Iterable[dict]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for result in results:
        for match in result.get("matches", []):
            source_id = match.get("source_id")
            excerpt = match.get("excerpt")
            if source_id and excerpt:
                sources[source_id] = str(excerpt)[:MAX_EXCERPT_CHARS]
        for record in result.get("records", []):
            source_id = record.get("source_id")
            excerpt = record.get("excerpt")
            if source_id and excerpt:
                sources[source_id] = str(excerpt)[:MAX_EXCERPT_CHARS]
        source_id = result.get("source_id")
        excerpt = result.get("excerpt")
        if source_id and excerpt:
            sources[source_id] = str(excerpt)[:MAX_EXCERPT_CHARS]
    return sources

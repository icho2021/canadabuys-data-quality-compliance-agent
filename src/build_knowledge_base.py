#!/usr/bin/env python3
"""Build a retrieval index over the official documentation the rules cite.

Chunks the CanadaBuys data dictionary, the 2026 restructuring notice and the
Treasury Board dictionary, then embeds each chunk with nomic-embed-text via
Ollama. Output is one Parquet file: at 123 chunks a vector database would add
operational surface for no measurable gain.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import duckdb
import requests

OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
MAX_CHARS = 1200


def embed(text: str) -> list[float]:
    response = requests.post(
        OLLAMA_EMBED_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=120
    )
    response.raise_for_status()
    return response.json()["embedding"]


def split_paragraphs(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    parts, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = re.sub(r"[ \t]+", " ", paragraph).strip()
        if not paragraph:
            continue
        if len(current) + len(paragraph) + 2 > max_chars and current:
            parts.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        parts.append(current)
    return parts


def controlled_values(pattern: str | None) -> list[str]:
    """Recover the published allowed-value list from a dictionary regex.

    Patterns look like '^$|^Active|Cancelled|Expired$'. Only alternations of
    literal text are value lists; patterns containing character classes are
    format constraints (dates, numbers) and carry no value list.
    """
    if not pattern or any(token in pattern for token in ("[", "\\d", "{")):
        return []
    values = []
    for alternative in pattern.split("|"):
        cleaned = alternative.strip().lstrip("^").rstrip("$").strip()
        if cleaned:
            values.append(cleaned)
    return values


def chunks_from_xml(path: Path) -> list[dict]:
    root = ET.parse(path).getroot()
    out = []
    for heading in root.iter("heading"):
        labels = [e.text.strip() for e in heading.findall("label") if e.text]
        descriptions = [e.text.strip() for e in heading.findall("description") if e.text]
        if not labels or not descriptions:
            continue
        data_type = (heading.findtext("data_type") or "").strip()
        pattern = (heading.findtext("data_pattern") or "").strip()
        allowed = controlled_values(pattern)
        body = [
            f"Field: {', '.join(labels)}",
            f"Official definition: {descriptions[0]}",
        ]
        if data_type:
            body.append(f"Data type: {data_type}")
        if pattern:
            body.append(f"Published validation pattern: {pattern}")
        if allowed:
            body.append(f"Published allowed values: {'; '.join(allowed)}")
        out.append(
            {
                "source_document": path.name,
                "section": labels[0],
                "field_names": labels,
                "controlled_values": allowed,
                "text": "\n".join(body),
            }
        )
    return out


def chunks_from_html(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return [
        {
            "source_document": path.name,
            "section": f"part {i + 1}",
            "field_names": [],
            "controlled_values": [],
            "text": chunk,
        }
        for i, chunk in enumerate(split_paragraphs(text))
        if len(chunk) > 120
    ]


def chunks_from_pdf(path: Path) -> list[dict]:
    from pypdf import PdfReader

    out = []
    for page_number, page in enumerate(PdfReader(str(path)).pages, 1):
        for i, chunk in enumerate(split_paragraphs(page.extract_text() or "")):
            if len(chunk) > 120:
                out.append(
                    {
                        "source_document": path.name,
                        "section": f"page {page_number} part {i + 1}",
                        "field_names": [],
                        "controlled_values": [],
                        "text": chunk,
                    }
                )
    return out


def chunks_from_xlsx(path: Path) -> list[dict]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out, current, label = [], [], "start"
    for row in workbook["EN"].iter_rows(values_only=True):
        cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if not cells:
            continue
        if cells[0] == "ID" and len(cells) > 1:
            if current:
                out.append({"section": label, "lines": current})
            label, current = cells[1], []
        current.append(" | ".join(cells))
    if current:
        out.append({"section": label, "lines": current})
    return [
        {
            "source_document": path.name,
            "section": entry["section"],
            "field_names": [entry["section"]],
            "controlled_values": [],
            "text": "\n".join(entry["lines"])[:MAX_CHARS],
        }
        for entry in out
        if len("\n".join(entry["lines"])) > 80
    ]


BUILDERS = {
    ".xml": chunks_from_xml,
    ".html": chunks_from_html,
    ".pdf": chunks_from_pdf,
    ".xlsx": chunks_from_xlsx,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    documents = [
        args.reference_dir / name
        for name in (
            "achatscanada-canadabuys-dd.xml",
            "COsoutien-CHsupport-eng.html",
            "canadabuys_data_dictionary.pdf",
            "proactive_contracts_dictionary.xlsx",
        )
        if (args.reference_dir / name).is_file()
    ]
    chunks = []
    for path in documents:
        produced = BUILDERS[path.suffix](path)
        chunks.extend(produced)
        print(f"  {path.name:<45} {len(produced):>4} chunks")

    print(f"\nEmbedding {len(chunks)} chunks with {EMBED_MODEL} ...")
    for i, chunk in enumerate(chunks, 1):
        chunk["embedding"] = embed(chunk["text"])
        if i % 50 == 0:
            print(f"  {i}/{len(chunks)}")
    dimensions = len(chunks[0]["embedding"])

    for i, chunk in enumerate(chunks):
        chunk["chunk_id"] = f"c{i:04d}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute("CREATE TABLE kb AS SELECT * FROM read_json_auto(?)", [
        str(_dump(chunks, args.output.with_suffix(".jsonl")))
    ])
    connection.execute(
        f"COPY (SELECT chunk_id, source_document, section, field_names, "
        f"controlled_values, text, embedding::FLOAT[{dimensions}] AS embedding "
        f"FROM kb) TO '{args.output}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    connection.close()
    args.output.with_suffix(".jsonl").unlink()

    with_values = sum(1 for c in chunks if c["controlled_values"])
    print(f"\n{len(chunks)} chunks, {dimensions}-dim embeddings -> {args.output}")
    print(f"{with_values} chunks carry a published controlled-value list")


def _dump(chunks: list[dict], path: Path) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return path


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Build a chunked RAG knowledge bundle for Open WebUI / Qwen.

This exporter walks a workshop-manual arc and, when available, its paired
wiring arc, converts HTML and PDF source documents, and writes overlapping
markdown chunks tuned for retrieval over car repair and diagnostic content.

Usage examples:
  python3 build_rag_knowledge.py --arc SEB
  python3 build_rag_knowledge.py --arc SEB --output-root rag_export
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

import build_obsidian_section as obsidian_export


ROOT = Path(__file__).resolve().parent
EXTRACTED_ROOT = ROOT / "extracted"
DEFAULT_OUTPUT_ROOT = ROOT / "rag_export"
DEFAULT_SOURCE_EXTS = {".htm", ".html", ".pdf"}
DEFAULT_TARGET_WORDS = 900
DEFAULT_OVERLAP_WORDS = 120


WIRING_ARC_BY_MANUAL_ARC = obsidian_export.WIRING_ARC_BY_MANUAL_ARC


@dataclass
class SourceDocument:
    arc: str
    source_file: str
    source_type: str
    title: str
    section_code: str
    section_title: str
    subsection_title: str
    markdown: str


@dataclass
class ChunkRecord:
    chunk_id: str
    arc: str
    source_arc: str
    source_file: str
    source_type: str
    source_path: str
    title: str
    section_code: str
    section_title: str
    subsection_title: str
    chunk_index: int
    chunk_count: int
    chunk_words: int
    text: str
    content_sha256: str


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_inline_images(markdown: str) -> str:
    return re.sub(r"!\[([^\]]*)\]\([^)]+\)", lambda m: m.group(1).strip(), markdown)


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    return slug or "chunk"


def _source_extensions(raw_value: str) -> set[str]:
    exts = set()
    for item in raw_value.split(","):
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        exts.add(ext)
    return exts or set(DEFAULT_SOURCE_EXTS)


def _chunk_settings(words: int, overlap: int) -> tuple[int, int]:
    if words < 300:
        words = 300
    if overlap < 0:
        overlap = 0
    if overlap >= words:
        overlap = max(1, words // 6)
    return words, overlap


def _get_markitdown():
    try:
        from markitdown import MarkItDown
    except ImportError as exc:  # pragma: no cover - runtime dependency check
        raise SystemExit(
            "Missing dependency 'markitdown'. Install with: "
            "python3 -m pip install 'markitdown[pdf]'"
        ) from exc
    return MarkItDown()


def _split_large_block(block: str, target_words: int) -> list[str]:
    block = _normalize_text(block)
    if not block:
        return []
    if _word_count(block) <= target_words:
        return [block]

    if "\n" in block:
        pieces = [line.strip() for line in block.splitlines() if line.strip()]
    else:
        pieces = [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", block) if piece.strip()]

    if not pieces:
        return [block]

    out: list[str] = []
    current: list[str] = []
    current_words = 0
    for piece in pieces:
        piece_words = _word_count(piece)
        if current and current_words + piece_words > target_words:
            out.append("\n".join(current).strip())
            current = []
            current_words = 0
        current.append(piece)
        current_words += piece_words
    if current:
        out.append("\n".join(current).strip())
    return [item for item in out if item.strip()]


def _iter_semantic_units(markdown: str, target_words: int) -> list[str]:
    markdown = _normalize_text(markdown)
    if not markdown:
        return []

    blocks: list[str] = []
    current: list[str] = []
    in_fence = False

    for line in markdown.splitlines():
        stripped = line.rstrip()
        fence = stripped.startswith("```")
        if fence:
            current.append(stripped)
            if in_fence:
                blocks.append("\n".join(current).strip())
                current = []
            in_fence = not in_fence
            continue

        if in_fence:
            current.append(stripped)
            continue

        if not stripped:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue

        if re.match(r"^#{1,6}\s+", stripped):
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            blocks.append(stripped)
            continue

        current.append(stripped)

    if current:
        blocks.append("\n".join(current).strip())

    units: list[str] = []
    for block in blocks:
        units.extend(_split_large_block(block, target_words))
    return [unit for unit in units if unit.strip()]


def _chunk_units(units: list[str], target_words: int, overlap_words: int) -> list[list[str]]:
    if not units:
        return []

    chunks: list[list[str]] = []
    current: list[str] = []
    current_words = 0

    for unit in units:
        unit_words = _word_count(unit)
        if current and current_words + unit_words > target_words:
            chunks.append(current)
            overlap: list[str] = []
            overlap_count = 0
            for previous in reversed(current):
                overlap.insert(0, previous)
                overlap_count += _word_count(previous)
                if overlap_count >= overlap_words:
                    break
            current = overlap[:]
            current_words = sum(_word_count(item) for item in current)
        current.append(unit)
        current_words += unit_words

    if current:
        chunks.append(current)
    return chunks


def _markdown_from_html(source_path: Path, all_files: set[str], wiring_arc: str | None) -> tuple[str, dict[str, str], str]:
    raw = source_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(raw, "html.parser")
    meta = obsidian_export._parse_meta(soup)
    title = obsidian_export._choose_title(soup, meta, source_path.stem)
    body = obsidian_export._clean_html_body(
        soup,
        available_lower=all_files,
        assets_to_copy=set(),
        wiring_arc=wiring_arc,
        wiring_available_lower=set(),
        wiring_assets_to_copy=set(),
    )
    markdown = obsidian_export._markdown_from_body(body)
    markdown = _strip_inline_images(markdown)
    return title, meta, markdown


def _markdown_from_pdf(converter, source_path: Path) -> tuple[str, str]:
    result = converter.convert(str(source_path))
    title = getattr(result, "title", "") or source_path.stem
    markdown = getattr(result, "markdown", "") or getattr(result, "text_content", "") or ""
    return title, _normalize_text(_strip_inline_images(markdown))


def _build_source_documents(
    arc: str,
    extracted_root: Path,
    source_exts: set[str],
    converter,
    wiring_arc: str | None,
) -> list[SourceDocument]:
    src_dir = extracted_root / arc
    if not src_dir.is_dir():
        raise SystemExit(f"Arc directory does not exist: {src_dir}")

    all_files = {p.name.lower() for p in src_dir.iterdir() if p.is_file()}
    source_files = sorted(
        p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in source_exts
    )

    documents: list[SourceDocument] = []
    for idx, source_path in enumerate(source_files, start=1):
        source_type = source_path.suffix.lower().lstrip(".")
        if source_path.suffix.lower() in {".htm", ".html"}:
            title, meta, markdown = _markdown_from_html(source_path, all_files, wiring_arc)
            section_code = obsidian_export._section_code(meta)
            section_title = (meta.get("tps_section_title") or "").strip() or "General"
            subsection_title = (meta.get("tps_ss_title") or "").strip() or "General"
        else:
            title, markdown = _markdown_from_pdf(converter, source_path)
            section_code = arc
            section_title = "General"
            subsection_title = "General"

        markdown = _normalize_text(markdown)
        if not markdown:
            continue

        documents.append(
            SourceDocument(
                arc=arc,
                source_file=source_path.name,
                source_type=source_type,
                title=title,
                section_code=section_code,
                section_title=section_title,
                subsection_title=subsection_title,
                markdown=markdown,
            )
        )

        if idx % 250 == 0:
            print(f"Converted {idx}/{len(source_files)} documents from {arc}...")

    return documents


def _chunk_document(
    out_dir: Path,
    doc: SourceDocument,
    arc: str,
    source_role: str,
    target_words: int,
    overlap_words: int,
) -> list[ChunkRecord]:
    units = _iter_semantic_units(doc.markdown, target_words)
    chunks = _chunk_units(units, target_words, overlap_words)
    total = len(chunks)
    chunk_records: list[ChunkRecord] = []

    for idx, chunk_units in enumerate(chunks, start=1):
        body = "\n\n".join(chunk_units).strip()
        if not body:
            continue
        chunk_words = _word_count(body)
        chunk_id = f"{source_role}-{doc.source_file}-{idx:03d}"
        header = [
            "---",
            f'arc: "{arc}"',
            f'source_arc: "{doc.arc}"',
            f'source_file: "{doc.source_file}"',
            f'source_type: "{doc.source_type}"',
            f'title: "{doc.title.replace("\"", "\\\"")}"',
            f'section_code: "{doc.section_code}"',
            f'section_title: "{doc.section_title.replace("\"", "\\\"")}"',
            f'subsection_title: "{doc.subsection_title.replace("\"", "\\\"")}"',
            f'chunk_id: "{chunk_id}"',
            f"chunk_index: {idx}",
            f"chunk_count: {total}",
            f"chunk_words: {chunk_words}",
            "---",
            "",
            f"# {doc.title}",
            "",
            f"> Source: {doc.arc}/{doc.source_file}",
            f"> Section: {doc.section_code} - {doc.section_title}",
            f"> Subsection: {doc.subsection_title}",
            f"> Chunk: {idx}/{total}",
            "",
            body,
            "",
        ]
        text = "\n".join(header)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        file_name = f"{_safe_slug(f'{doc.arc}-{doc.source_file}')}-{idx:03d}.md"
        (out_dir / file_name).write_text(text, encoding="utf-8")
        chunk_records.append(
            ChunkRecord(
                chunk_id=chunk_id,
                arc=arc,
                source_arc=doc.arc,
                source_file=doc.source_file,
                source_type=doc.source_type,
                source_path=f"{doc.arc}/{doc.source_file}",
                title=doc.title,
                section_code=doc.section_code,
                section_title=doc.section_title,
                subsection_title=doc.subsection_title,
                chunk_index=idx,
                chunk_count=total,
                chunk_words=chunk_words,
                text=text,
                content_sha256=digest,
            )
        )

    return chunk_records


def export_rag_knowledge(
    arc: str,
    extracted_root: Path,
    output_root: Path,
    source_exts: set[str],
    include_wiring: bool,
    target_words: int,
    overlap_words: int,
) -> Path:
    converter = _get_markitdown()
    primary_arc = arc.upper().strip()
    paired_wiring_arc = WIRING_ARC_BY_MANUAL_ARC.get(primary_arc) if include_wiring else None

    out_dir = output_root / primary_arc
    chunks_dir = out_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    source_documents: list[SourceDocument] = []
    source_documents.extend(
        _build_source_documents(primary_arc, extracted_root, source_exts, converter, paired_wiring_arc)
    )
    if paired_wiring_arc:
        source_documents.extend(
            _build_source_documents(paired_wiring_arc, extracted_root, source_exts, converter, None)
        )

    all_chunks: list[ChunkRecord] = []
    for doc in source_documents:
        all_chunks.extend(
            _chunk_document(
                chunks_dir,
                doc,
                primary_arc,
                "wiring" if doc.arc == paired_wiring_arc else "manual",
                target_words,
                overlap_words,
            )
        )

    corpus_path = out_dir / "chunks.jsonl"
    with corpus_path.open("w", encoding="utf-8") as handle:
        for record in all_chunks:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    manifest = {
        "arc": primary_arc,
        "pairedWiringArc": paired_wiring_arc,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceExtensions": sorted(source_exts),
        "sourceDocumentCount": len(source_documents),
        "chunkCount": len(all_chunks),
        "targetWords": target_words,
        "overlapWords": overlap_words,
        "corpusFile": corpus_path.name,
        "chunkDirectory": "chunks",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Done. Output: {out_dir}")
    print(f"Source documents: {len(source_documents)}")
    print(f"Chunks written: {len(all_chunks)}")
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arc", required=True, help="Arc code, e.g. SEB")
    parser.add_argument(
        "--extracted-root",
        default=str(EXTRACTED_ROOT),
        help=f"Source extracted root (default: {EXTRACTED_ROOT})",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"Output root for the RAG bundle (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--extensions",
        default=",".join(sorted(DEFAULT_SOURCE_EXTS)),
        help="Comma-separated file extensions to include (default: HTML, PDF)",
    )
    parser.add_argument(
        "--no-wiring",
        action="store_true",
        help="Do not include the paired wiring arc, even if one is known.",
    )
    parser.add_argument(
        "--target-words",
        type=int,
        default=DEFAULT_TARGET_WORDS,
        help=f"Target words per chunk (default: {DEFAULT_TARGET_WORDS})",
    )
    parser.add_argument(
        "--overlap-words",
        type=int,
        default=DEFAULT_OVERLAP_WORDS,
        help=f"Overlap words between chunks (default: {DEFAULT_OVERLAP_WORDS})",
    )
    args = parser.parse_args()

    arc = args.arc.upper().strip()
    target_words, overlap_words = _chunk_settings(args.target_words, args.overlap_words)
    export_rag_knowledge(
        arc,
        Path(args.extracted_root),
        Path(args.output_root),
        _source_extensions(args.extensions),
        include_wiring=not args.no_wiring,
        target_words=target_words,
        overlap_words=overlap_words,
    )


if __name__ == "__main__":
    main()
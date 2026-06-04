#!/usr/bin/env python3
"""
Build a section-level training corpus from extracted Ford workshop manual files.

The exporter walks one arc directory under extracted/, converts the supported
source documents with MarkItDown, and writes a JSONL corpus plus a manifest.

Usage examples:
  python3 build_training_data.py --arc SEB
  python3 build_training_data.py --arc SEB --output-root training_export
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
DEFAULT_OUTPUT_ROOT = ROOT / "training_export"
DEFAULT_SOURCE_EXTS = {".htm", ".html", ".pdf", ".wcf", ".xml", ".txt"}


@dataclass
class TrainingRecord:
    arc: str
    source_file: str
    source_path: str
    source_type: str
    title: str
    section_code: str
    section_title: str
    subsection_title: str
    markitdown_title: str
    text: str
    content_sha256: str


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _markdown_from_result(result: object) -> str:
    for attr in ("markdown", "text_content"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return _normalize_text(value)
    return ""


def _get_markitdown():
    try:
        from markitdown import MarkItDown
    except ImportError as exc:  # pragma: no cover - runtime dependency check
        raise SystemExit(
            "Missing dependency 'markitdown'. Install with: "
            "python3 -m pip install 'markitdown[pdf]'"
        ) from exc
    return MarkItDown()


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


def _convert_file(converter, source_path: Path) -> tuple[str, str]:
    result = converter.convert(str(source_path))
    return getattr(result, "title", "") or "", _markdown_from_result(result)


def export_training_data(
    arc: str,
    extracted_root: Path,
    output_root: Path,
    source_exts: set[str],
) -> Path:
    src_dir = extracted_root / arc
    if not src_dir.is_dir():
        raise SystemExit(f"Arc directory does not exist: {src_dir}")

    out_dir = output_root / arc
    out_dir.mkdir(parents=True, exist_ok=True)

    converter = _get_markitdown()
    source_files = sorted(
        p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in source_exts
    )

    records: list[TrainingRecord] = []
    skipped: list[dict[str, str]] = []

    for idx, source_path in enumerate(source_files, start=1):
        source_name = source_path.name
        source_rel = f"{arc}/{source_name}"
        source_type = source_path.suffix.lower().lstrip(".")

        try:
            converted_title, text = _convert_file(converter, source_path)
        except Exception as exc:  # pragma: no cover - runtime conversion failures
            skipped.append({"source_file": source_name, "error": f"{type(exc).__name__}: {exc}"})
            continue

        if not text:
            skipped.append({"source_file": source_name, "error": "empty conversion result"})
            continue

        if source_path.suffix.lower() in {".htm", ".html"}:
            raw = source_path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(raw, "html.parser")
            meta = obsidian_export._parse_meta(soup)
            title = obsidian_export._choose_title(soup, meta, source_path.stem)
            section_code = obsidian_export._section_code(meta)
            section_title = (meta.get("tps_section_title") or "").strip() or "General"
            subsection_title = (meta.get("tps_ss_title") or "").strip() or "General"
        else:
            title = converted_title.strip() or source_path.stem
            section_code = "Uncategorized"
            section_title = "General"
            subsection_title = "General"

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        records.append(
            TrainingRecord(
                arc=arc,
                source_file=source_name,
                source_path=source_rel,
                source_type=source_type,
                title=title,
                section_code=section_code,
                section_title=section_title,
                subsection_title=subsection_title,
                markitdown_title=converted_title.strip(),
                text=text,
                content_sha256=digest,
            )
        )

        if idx % 200 == 0:
            print(f"Converted {idx}/{len(source_files)} documents...")

    corpus_path = out_dir / "training_corpus.jsonl"
    with corpus_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    manifest = {
        "arc": arc,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceExtensions": sorted(source_exts),
        "sourceCount": len(source_files),
        "recordCount": len(records),
        "skippedCount": len(skipped),
        "corpusFile": corpus_path.name,
        "skipped": skipped,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Done. Output: {out_dir}")
    print(f"Records written: {len(records)}")
    print(f"Skipped: {len(skipped)}")
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
        help=f"Output root for the training bundle (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--extensions",
        default=",".join(sorted(DEFAULT_SOURCE_EXTS)),
        help="Comma-separated file extensions to include (default: HTML, PDF, WCF, XML, TXT)",
    )
    args = parser.parse_args()

    arc = args.arc.upper().strip()
    export_training_data(
        arc,
        Path(args.extracted_root),
        Path(args.output_root),
        _source_extensions(args.extensions),
    )


if __name__ == "__main__":
    main()
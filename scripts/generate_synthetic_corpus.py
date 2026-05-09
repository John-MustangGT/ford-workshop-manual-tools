#!/usr/bin/env python3
"""Generate a synthetic, non-proprietary Ford-manual-like test corpus.

This utility intentionally writes only synthetic fixtures (no Ford data) so the
repository can have safe regression assets for parser/server verification.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

IDICOMP_MAGIC = b"\x01IDICOMP\x01"
BAY_POD_MAGIC = b"BAY POD\x02"
POD_BAY_MAGIC = b"POD BAY\x01"


def _u24le(value: int) -> bytes:
    return bytes((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))


def _literal_only_lzss(payload: bytes) -> bytes:
    """Encode bytes as IDICOMP literal-only chunks (valid for this decompressor)."""
    out = bytearray()
    pos = 0
    while pos < len(payload):
        block = payload[pos:pos + 16]
        out.extend(b"\x00\x00")
        out.extend(block)
        pos += len(block)
    return bytes(out)


def make_idicomp_chunked(*chunks: bytes) -> bytes:
    """Build an IDICOMP block with one or more chunked literal-compressed chunks."""
    out = bytearray(IDICOMP_MAGIC)
    for chunk in chunks:
        packed = _literal_only_lzss(chunk)
        out.extend(len(packed).to_bytes(2, "little"))
        out.extend(packed)
    out.extend(b"\x00\x00")
    return bytes(out)


def make_idicomp_raw_passthrough(payload: bytes) -> bytes:
    """Build an IDICOMP wrapper that triggers raw passthrough-like behavior.

    Uses a standard chunk-length wrapper, but the chunk bytes are intentionally
    non-LZSS for this decoder. That causes extractor fallback logic to append the
    raw chunk bytes directly, preserving payload signatures.
    """
    return IDICOMP_MAGIC + len(payload).to_bytes(2, "little") + payload + b"\x00\x00"


def make_bay_pod_arc(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a tiny BAY POD archive with a valid filename table and directory."""
    filename_table = bytearray()
    name_meta: list[tuple[int, int, str, bytes]] = []

    for name, block in entries:
        encoded = name.encode("ascii")
        off = len(filename_table)
        filename_table.extend(encoded)
        name_meta.append((off, len(encoded), name, block))

    num_entries = len(entries)
    data_start = 16 + num_entries * 16 + 1 + len(filename_table)
    current_data_off = data_start

    directory = bytearray()
    data_blocks = bytearray()

    for name_off, name_len, _name, block in name_meta:
        entry = bytearray(16)
        entry[0] = 0
        entry[1:4] = _u24le(name_off)
        entry[4] = 0
        entry[5] = name_len
        entry[6:9] = b"\x00\x00\x00"
        entry[9:13] = current_data_off.to_bytes(4, "little")
        entry[13:15] = len(block).to_bytes(2, "little")
        entry[15] = 0
        directory.extend(entry)

        data_blocks.extend(block)
        current_data_off += len(block)

    return (
        BAY_POD_MAGIC
        + b"\x00"
        + _u24le(num_entries)
        + b"\x00"
        + _u24le(len(filename_table))
        + directory
        + b"\x00"
        + bytes(filename_table)
        + bytes(data_blocks)
    )


def make_pod_bay_arc(blocks: list[bytes]) -> bytes:
    """Build a tiny POD BAY archive containing sequential IDICOMP blocks."""
    return POD_BAY_MAGIC + b"\x00" * 8 + b"".join(blocks)


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def generate_corpus(output_dir: Path) -> None:
    """Write synthetic fixtures useful for extractor/parser/server verification."""
    arcs_dir = output_dir / "arcs"
    idicomp_dir = output_dir / "idicomp"
    extracted_dir = output_dir / "extracted"

    alpha_index = b"<html><body><h1>Synthetic Mustang Manual</h1><a href='r98s20.htm'>Open page</a></body></html>"
    html_page = b"<html><body><h2>Synthetic Procedure</h2><p>No Ford content included.</p></body></html>"
    svg_payload = b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 20'><text x='2' y='14'>EEB042001</text></svg>"
    xml_payload = b"<workunit>\n  <filename>r98s20.htm</filename>\n</workunit>"
    css_payload = b"body { font-family: Arial; background: #fff; }"

    jpg_payload = b"\xff\xd8\xff\xe0SYNTHETIC-JPG"
    gif_payload = b"GIF89aSYNTHETIC-GIF"
    pdf_payload = b"%PDF-1.4\n% synthetic pdf\n"
    wcf_payload = b"; synthetic wcf control text\n"

    idicomp_samples = {
        "chunked_html.idicomp": make_idicomp_chunked(alpha_index[:40], alpha_index[40:]),
        "raw_jpg.idicomp": make_idicomp_raw_passthrough(jpg_payload),
        "raw_gif.idicomp": make_idicomp_raw_passthrough(gif_payload),
        "raw_pdf.idicomp": make_idicomp_raw_passthrough(pdf_payload),
        "raw_wcf.idicomp": make_idicomp_raw_passthrough(wcf_payload),
        "compressed_svg.idicomp": make_idicomp_chunked(svg_payload),
        "compressed_xml.idicomp": make_idicomp_chunked(xml_payload),
        "compressed_css.idicomp": make_idicomp_chunked(css_payload),
    }
    for name, blob in idicomp_samples.items():
        write_bytes(idicomp_dir / name, blob)

    bay_entries = [
        ("SEBALPHAINDEX.HTM", make_idicomp_chunked(alpha_index[:40], alpha_index[40:])),
        ("r98s20.htm", make_idicomp_chunked(html_page)),
        ("EEB042001.SVG", make_idicomp_chunked(svg_payload)),
        ("WORKUNIT.XML", make_idicomp_chunked(xml_payload)),
        ("STYLE.CSS", make_idicomp_chunked(css_payload)),
        ("PHOTO.JPG", make_idicomp_raw_passthrough(jpg_payload)),
        ("ICON.GIF", make_idicomp_raw_passthrough(gif_payload)),
        ("BOOK.PDF", make_idicomp_raw_passthrough(pdf_payload)),
        ("META.WCF", make_idicomp_raw_passthrough(wcf_payload)),
    ]
    write_bytes(arcs_dir / "SEB.arc", make_bay_pod_arc(bay_entries))

    pod_blocks = [
        make_idicomp_chunked(xml_payload),
        make_idicomp_chunked(html_page[:30], html_page[30:]),
        make_idicomp_chunked(css_payload),
        make_idicomp_raw_passthrough(gif_payload),
        make_idicomp_raw_passthrough(jpg_payload),
        make_idicomp_raw_passthrough(pdf_payload),
        make_idicomp_raw_passthrough(wcf_payload),
    ]
    write_bytes(arcs_dir / "R98.arc", make_pod_bay_arc(pod_blocks))

    # Minimal extracted tree for serve.py routing/manual verification.
    write_bytes(extracted_dir / "SEB" / "SEBALPHAINDEX.HTM", alpha_index)
    write_bytes(extracted_dir / "SEB" / "r98s20.htm", html_page)
    write_bytes(extracted_dir / "SEB" / "style.css", css_payload)
    write_bytes(extracted_dir / "EEB" / "EEB042001.SVG", svg_payload)
    write_bytes(extracted_dir / "R98" / "r98s20.htm", html_page)

    write_bytes(
        output_dir / "SYNTHETIC_NOTICE.txt",
        (
            b"This corpus is synthetic and non-proprietary.\n"
            b"It is intended for safe regression testing of archive parsing,\n"
            b"IDICOMP handling, and local server routing behavior.\n"
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate synthetic regression fixtures for ford-workshop-manual-tools"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="testdata/synthetic_corpus",
        help="Output directory (default: testdata/synthetic_corpus)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete output directory first if it already exists",
    )
    args = parser.parse_args()

    out = Path(args.output).resolve()
    if out.exists() and args.overwrite:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    generate_corpus(out)
    print(f"Synthetic corpus written to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Export an extracted workshop-manual arc (for example SEB) into an
Obsidian-friendly vault structure.

Pipeline:
1) Parse and clean legacy HTML.
2) Convert cleaned HTML to Markdown.
3) Rewrite internal/manual links and copy referenced assets (JPG/PDF/GIF/SVG).
4) Build section and alphabetical index notes for Obsidian navigation.

Usage examples:
  python3 build_obsidian_section.py --arc SEB
  python3 build_obsidian_section.py --arc SEB --output-root obsidian_export
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise SystemExit(
        "Missing dependency 'beautifulsoup4'. Install with: "
        "python3 -m pip install beautifulsoup4"
    ) from exc

try:
    from markdownify import markdownify as md
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise SystemExit(
        "Missing dependency 'markdownify'. Install with: "
        "python3 -m pip install markdownify"
    ) from exc


ROOT = Path(__file__).resolve().parent
EXTRACTED_ROOT = ROOT / "extracted"

ASSET_EXTS = {".jpg", ".jpeg", ".gif", ".png", ".svg", ".pdf"}

# Workshop manual arc -> wiring arc
WIRING_ARC_BY_MANUAL_ARC = {
    "SE2": "EE2",
    "SCA": "EEA",
    "SEB": "EEB",
    "SED": "EED",
    "SEF": "EEF",
    "SDG": "EEG",
    "SEH": "EEH",
    "SEI": "EEI",
    "SEJ": "EEJ",
    "SEK": "EEK",
    "SEM": "EEM",
    "SEN": "EEN",
    "SEO": "EEO",
    "SEP": "EEP",
    "SER": "EER",
    "SDS": "EES",
    "SDZ": "EET",
    "SEV": "EEV",
    "SCY": "EEY",
}


@dataclass
class PageRecord:
    source_name: str
    md_name: str
    title: str
    section_code: str
    section_title: str
    subsection_title: str


def _text(node: Tag | None) -> str:
    if not node:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _safe_filename(name: str) -> str:
    sanitized = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return sanitized or "Untitled"


def _parse_meta(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        name = (tag.get("name") or "").strip().lower()
        if not name:
            continue
        content = (tag.get("content") or "").strip()
        if name.startswith("tps_") and name not in meta:
            meta[name] = content
    return meta


def _section_code(meta: dict[str, str]) -> str:
    system = (meta.get("tps_system") or "").strip()
    group = (meta.get("tps_group") or "").strip()
    section = (meta.get("tps_section") or "").strip()
    if system and group and section:
        return f"{system}{group}-{section}"
    return "Uncategorized"


def _choose_title(soup: BeautifulSoup, meta: dict[str, str], fallback_name: str) -> str:
    for key in ("tps_proctitle", "tps_ss_title"):
        value = (meta.get(key) or "").strip()
        if value:
            return value

    for selector in ("h1", "h2", "h3", "h4"):
        heading = soup.find(selector)
        if heading:
            text = _text(heading)
            if text:
                return text

    title_tag = soup.find("title")
    if title_tag:
        text = _text(title_tag)
        if text:
            return text

    return fallback_name


def _rewrite_href(
    raw_href: str,
    available_lower: set[str],
    assets_to_copy: set[str],
    wiring_arc: str | None,
    wiring_available_lower: set[str],
    wiring_assets_to_copy: set[str],
) -> str:
    href = raw_href.strip()
    if not href:
        return href

    parsed = urlparse(href)
    if parsed.scheme or parsed.netloc:
        return href

    path = parsed.path
    query = parsed.query
    fragment = parsed.fragment

    if path.lower().endswith("ep_main.asp") and wiring_arc:
        qs = parse_qs(query)
        cell = ""
        for key in ("cell", "CELL"):
            vals = qs.get(key) or []
            if vals:
                cell = vals[0].strip()
                break
        if cell:
            wanted = f"{wiring_arc}{cell}.svg"
            if wanted.lower() in wiring_available_lower:
                wiring_assets_to_copy.add(wanted)
                return f"../wiring_assets/{wanted}"

    if path.lower().endswith("2colframeset.asp"):
        qs = parse_qs(query)
        target = ""
        for key in ("rightside", "leftside"):
            vals = qs.get(key) or []
            if vals:
                target = vals[0]
                break
        if target:
            stem = Path(target).stem
            suffix = f"#{fragment}" if fragment else ""
            return f"{stem}.md{suffix}"
        return href

    file_name = Path(path).name
    if not file_name:
        return href

    lower_name = file_name.lower()
    if lower_name not in available_lower:
        if lower_name in wiring_available_lower:
            wiring_assets_to_copy.add(file_name)
            return f"../wiring_assets/{file_name}"
        return href

    ext = Path(file_name).suffix.lower()
    suffix = f"#{fragment}" if fragment else ""
    if ext in {".htm", ".html"}:
        return f"{Path(file_name).stem}.md{suffix}"
    if ext in ASSET_EXTS:
        assets_to_copy.add(file_name)
        return f"../assets/{file_name}{suffix}"

    return href


def _clean_html_body(
    soup: BeautifulSoup,
    available_lower: set[str],
    assets_to_copy: set[str],
    wiring_arc: str | None,
    wiring_available_lower: set[str],
    wiring_assets_to_copy: set[str],
) -> Tag:
    body = soup.find("body")
    if body is None:
        body = soup

    for tag in body.find_all(["script", "style", "meta", "link"]):
        tag.decompose()

    # Some source pages place <th> directly under <table> instead of a header <tr>.
    # markdownify handles those poorly, so wrap direct header cells into a proper row first.
    for table in body.find_all("table"):
        direct_header_cells = [
            child
            for child in list(table.children)
            if isinstance(child, Tag) and child.name == "th"
        ]
        if direct_header_cells:
            header_row = soup.new_tag("tr")
            for th in direct_header_cells:
                header_row.append(th.extract())
            first_row = table.find("tr")
            if first_row:
                first_row.insert_before(header_row)
            else:
                table.insert(0, header_row)

    # Ford pages often wrap standalone IMG tags inside <ul>; unwrap for cleaner markdown.
    for ul in body.find_all("ul"):
        non_ws = [c for c in ul.contents if not isinstance(c, NavigableString) or c.strip()]
        if non_ws and all(isinstance(c, Tag) and c.name == "img" for c in non_ws):
            ul.unwrap()

    # Preserve explicit step numbers from repeated <ol start="N"><li>..</li></ol> blocks.
    for ol in list(body.find_all("ol")):
        start = (ol.get("start") or "").strip()
        direct_li = [c for c in ol.children if isinstance(c, Tag) and c.name == "li"]
        if start and len(direct_li) == 1 and start.isdigit():
            p = soup.new_tag("p")
            lead = soup.new_tag("strong")
            lead.string = f"{start}. "
            p.append(lead)
            li = direct_li[0]
            for child in list(li.contents):
                p.append(child.extract())
            ol.replace_with(p)

    for span in body.find_all("span"):
        if span.get("class") and "popup" in span.get("class"):
            span.unwrap()

    for tag in body.find_all(True):
        keep_attrs: dict[str, str] = {}
        if tag.name == "a" and tag.has_attr("href"):
            keep_attrs["href"] = _rewrite_href(
                tag.get("href", ""),
                available_lower,
                assets_to_copy,
                wiring_arc,
                wiring_available_lower,
                wiring_assets_to_copy,
            )
        if tag.name == "img" and tag.has_attr("src"):
            src = tag.get("src", "")
            file_name = Path(urlparse(src).path).name
            if file_name:
                assets_to_copy.add(file_name)
                keep_attrs["src"] = f"../assets/{file_name}"
            else:
                keep_attrs["src"] = src
            alt = (tag.get("alt") or "").strip()
            if alt:
                keep_attrs["alt"] = alt
        if tag.name == "a":
            title = (tag.get("title") or "").strip()
            if title:
                keep_attrs["title"] = title
        tag.attrs = keep_attrs

    return body


def _markdown_from_body(body: Tag) -> str:
    html = str(body)
    text = md(
        html,
        heading_style="ATX",
        bullets="-",
        strip=["style", "script"],
    )
    text = _normalize_pipe_tables(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text + "\n"


def _normalize_pipe_tables(markdown: str) -> str:
    """Repair common malformed pipe-table output from legacy HTML."""
    lines = markdown.splitlines()

    # Split lines where markdownify collapsed "header || first_row" into one line.
    expanded: list[str] = []
    for line in lines:
        stripped = line.strip()
        if (
            "||" in stripped
            and "|" in stripped
            and not stripped.startswith("|")
            and stripped.count("|") >= 4
        ):
            left, right = stripped.split("||", 1)
            left = left.strip()
            right = right.strip()
            if left:
                if not left.startswith("|"):
                    left = "| " + left
                if not left.endswith("|"):
                    left = left + " |"
                expanded.append(left)
            if right:
                if not right.startswith("|"):
                    right = "| " + right
                if not right.endswith("|"):
                    right = right + " |"
                expanded.append(right)
            continue
        expanded.append(line)

    sep_re = re.compile(r"^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$")

    def looks_like_table_row(text: str) -> bool:
        s = text.strip()
        return s.startswith("|") and s.count("|") >= 2

    def cell_count(row: str) -> int:
        s = row.strip()
        core = s.strip("|")
        if not core.strip():
            return 0
        return len([c for c in core.split("|")])

    fixed: list[str] = []
    i = 0
    while i < len(expanded):
        line = expanded[i]
        if not looks_like_table_row(line):
            fixed.append(line)
            i += 1
            continue

        block_start = i
        while i < len(expanded) and looks_like_table_row(expanded[i]):
            i += 1
        block = expanded[block_start:i]

        if len(block) >= 2 and not sep_re.match(block[1].strip()):
            cols = max(cell_count(block[0]), 2)
            separator = "| " + " | ".join(["---"] * cols) + " |"
            block.insert(1, separator)

        fixed.extend(block)

    return "\n".join(fixed)


def _convert_local_md_links_to_wikilinks(markdown: str) -> str:
    # Convert [Label](SEB3E008.md) -> [[SEB3E008|Label]]
    pattern = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")

    def repl(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        target = match.group(2).strip()
        if not target:
            return match.group(0)
        if target.startswith(("http://", "https://", "mailto:")):
            return match.group(0)

        if "#" in target:
            path, anchor = target.split("#", 1)
        else:
            path, anchor = target, ""

        if not path.lower().endswith(".md"):
            return match.group(0)

        stem = Path(path).stem
        if anchor:
            stem = f"{stem}#{anchor}"

        if label and label != stem:
            return f"[[{stem}|{label}]]"
        return f"[[{stem}]]"

    return pattern.sub(repl, markdown)


def _wikilink(label: str, target_md_path: str) -> str:
    stem = Path(target_md_path).stem
    if label == stem:
        return f"[[{stem}]]"
    return f"[[{stem}|{label}]]"


def _build_frontmatter(meta: dict[str, str], page: PageRecord) -> str:
    lines = [
        "---",
        f'title: "{page.title.replace("\"", "\\\"")}"',
        f'source_file: "{page.source_name}"',
        f'arc: "{(meta.get("tps_bookcode") or "").strip()}"',
        f'section_code: "{page.section_code}"',
        f'section_title: "{page.section_title.replace("\"", "\\\"")}"',
        f'subsection_title: "{page.subsection_title.replace("\"", "\\\"")}"',
        "---",
        "",
    ]
    return "\n".join(lines)


def _write_alphabetical_index(pages: list[PageRecord], out_path: Path) -> None:
    grouped: dict[str, list[PageRecord]] = defaultdict(list)
    for page in pages:
        first = (page.title.strip()[:1] or "#").upper()
        key = first if first.isalnum() else "#"
        grouped[key].append(page)

    lines = ["# Alphabetical Index", ""]
    for letter in sorted(grouped.keys()):
        lines.append(f"## {letter}")
        for rec in sorted(grouped[letter], key=lambda r: (r.title.lower(), r.source_name)):
            lines.append(f"- {_wikilink(rec.title, rec.md_name)}")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _write_section_indexes(
    pages: list[PageRecord],
    out_root: Path,
) -> None:
    sections_dir = out_root / "indexes" / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    grouped: dict[tuple[str, str], list[PageRecord]] = defaultdict(list)
    for page in pages:
        grouped[(page.section_code, page.section_title)].append(page)

    summary_lines = ["# Section Index", ""]

    for (section_code, section_title), records in sorted(grouped.items()):
        records.sort(key=lambda r: (r.subsection_title.lower(), r.title.lower(), r.source_name))
        base_name = _safe_filename(f"{section_code} {section_title}".strip())
        section_file = sections_dir / f"{base_name}.md"

        summary_lines.append(
            f"- {_wikilink(f'{section_code} - {section_title}', section_file.name)} ({len(records)} pages)"
        )

        lines = [f"# {section_code} - {section_title}", ""]
        by_sub: dict[str, list[PageRecord]] = defaultdict(list)
        for rec in records:
            sub = rec.subsection_title or "General"
            by_sub[sub].append(rec)

        for subsection in sorted(by_sub.keys()):
            lines.append(f"## {subsection}")
            for rec in sorted(by_sub[subsection], key=lambda r: (r.title.lower(), r.source_name)):
                lines.append(f"- {_wikilink(rec.title, rec.md_name)}")
            lines.append("")

        section_file.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    (out_root / "indexes" / "Section Index.md").write_text(
        "\n".join(summary_lines).strip() + "\n",
        encoding="utf-8",
    )


def _write_root_index(arc: str, out_root: Path, pages: list[PageRecord], copied_assets: set[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"# {arc} Obsidian Manual Bundle",
        "",
        f"Generated: {now}",
        f"Pages: {len(pages)}",
        f"All arc assets copied: {len(copied_assets)}",
        "",
        "## Navigation",
        "- [[Section Index]]",
        "- [[Alphabetical Index]]",
        "",
    ]
    (out_root / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _zip_vault(vault_dir: Path) -> Path:
    """Zip the finished vault directory next to it and return the zip path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = vault_dir.parent / f"{vault_dir.name}_{ts}.zip"
    print(f"Zipping vault to {zip_path} ...")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(vault_dir.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(vault_dir.parent))
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Zip created: {zip_path.name} ({size_mb:.1f} MB)")
    return zip_path


def export_arc(
    arc: str,
    extracted_root: Path,
    output_root: Path,
    wiring_arc: str | None,
    copy_all_wiring_assets: bool,
) -> Path:
    src_dir = extracted_root / arc
    if not src_dir.is_dir():
        raise SystemExit(f"Arc directory does not exist: {src_dir}")

    out_root = output_root / arc
    pages_dir = out_root / "pages"
    assets_dir = out_root / "assets"
    wiring_assets_dir = out_root / "wiring_assets"
    indexes_dir = out_root / "indexes"
    meta_dir = out_root / "_meta"

    pages_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    wiring_assets_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    all_files = [p.name for p in src_dir.iterdir() if p.is_file()]
    available_lower = {name.lower() for name in all_files}

    wiring_dir = extracted_root / wiring_arc if wiring_arc else None
    wiring_files: list[str] = []
    wiring_available_lower: set[str] = set()
    if wiring_dir and wiring_dir.is_dir():
        wiring_files = [p.name for p in wiring_dir.iterdir() if p.is_file()]
        wiring_available_lower = {name.lower() for name in wiring_files}

    html_files = sorted(
        [name for name in all_files if Path(name).suffix.lower() in {".htm", ".html"}]
    )

    pages: list[PageRecord] = []
    assets_to_copy: set[str] = set()
    wiring_assets_to_copy: set[str] = set()

    for idx, file_name in enumerate(html_files, start=1):
        src_path = src_dir / file_name
        raw = src_path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(raw, "html.parser")

        meta = _parse_meta(soup)
        title = _choose_title(soup, meta, Path(file_name).stem)
        section_code = _section_code(meta)
        section_title = (meta.get("tps_section_title") or "").strip() or "General"
        subsection_title = (meta.get("tps_ss_title") or "").strip() or "General"

        body = _clean_html_body(
            soup,
            available_lower=available_lower,
            assets_to_copy=assets_to_copy,
            wiring_arc=wiring_arc,
            wiring_available_lower=wiring_available_lower,
            wiring_assets_to_copy=wiring_assets_to_copy,
        )
        markdown = _markdown_from_body(body)
        markdown = _convert_local_md_links_to_wikilinks(markdown)

        md_name = f"{Path(file_name).stem.upper()}.md"
        record = PageRecord(
            source_name=file_name,
            md_name=md_name,
            title=title,
            section_code=section_code,
            section_title=section_title,
            subsection_title=subsection_title,
        )
        pages.append(record)

        content = _build_frontmatter(meta, record) + markdown
        (pages_dir / md_name).write_text(content, encoding="utf-8")

        if idx % 400 == 0:
            print(f"Converted {idx}/{len(html_files)} HTML files...")

    copied_assets: set[str] = set()
    lower_file_map = {name.lower(): name for name in all_files}
    all_asset_files = [name for name in all_files if Path(name).suffix.lower() in ASSET_EXTS]
    for requested in sorted(set(all_asset_files) | assets_to_copy):
        key = requested.lower()
        source_name = lower_file_map.get(key)
        if not source_name:
            continue
        src = src_dir / source_name
        if not src.is_file():
            continue
        shutil.copy2(src, assets_dir / source_name)
        copied_assets.add(source_name)

    copied_wiring_assets: set[str] = set()
    wiring_lower_map = {name.lower(): name for name in wiring_files}
    all_wiring_asset_files = [name for name in wiring_files if Path(name).suffix.lower() in ASSET_EXTS]
    requested_wiring = set(all_wiring_asset_files) if copy_all_wiring_assets else set(wiring_assets_to_copy)
    for requested in sorted(requested_wiring):
        key = requested.lower()
        source_name = wiring_lower_map.get(key)
        if not source_name or not wiring_dir:
            continue
        src = wiring_dir / source_name
        if not src.is_file():
            continue
        shutil.copy2(src, wiring_assets_dir / source_name)
        copied_wiring_assets.add(source_name)

    _write_alphabetical_index(pages, indexes_dir / "Alphabetical Index.md")
    _write_section_indexes(pages, out_root)
    _write_root_index(arc, out_root, pages, copied_assets)

    manifest = {
        "arc": arc,
        "wiringArc": wiring_arc,
        "copyAllWiringAssets": copy_all_wiring_assets,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "pageCount": len(pages),
        "copiedAssetCount": len(copied_assets),
        "copiedWiringAssetCount": len(copied_wiring_assets),
        "assets": sorted(copied_assets),
        "wiringAssets": sorted(copied_wiring_assets),
    }
    (meta_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Done. Output: {out_root}")
    print(f"Pages written: {len(pages)}")
    print(f"Assets copied: {len(copied_assets)}")
    print(f"Wiring assets copied: {len(copied_wiring_assets)}")

    return out_root


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
        default=str(ROOT / "obsidian_export"),
        help="Output root for Obsidian bundle",
    )
    parser.add_argument(
        "--wiring-arc",
        default=None,
        help="Optional wiring arc (for example EEB). If omitted, inferred by arc code.",
    )
    parser.add_argument(
        "--copy-all-wiring-assets",
        action="store_true",
        help="Copy all wiring-arc assets instead of only referenced wiring assets.",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="Zip the finished vault into a timestamped .zip file next to the output folder.",
    )
    args = parser.parse_args()

    arc = args.arc.upper().strip()
    wiring_arc = args.wiring_arc.upper().strip() if args.wiring_arc else WIRING_ARC_BY_MANUAL_ARC.get(arc)
    vault_dir = export_arc(
        arc,
        Path(args.extracted_root),
        Path(args.output_root),
        wiring_arc=wiring_arc,
        copy_all_wiring_assets=args.copy_all_wiring_assets,
    )
    if args.zip:
        _zip_vault(vault_dir)


if __name__ == "__main__":
    main()

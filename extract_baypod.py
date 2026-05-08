#!/usr/bin/env python3
"""
Ford Workshop Manual BAY POD arc extractor with IDICOMP decompressor.

BAY POD format structure:
  Offset 0-7:  Magic "BAY POD\x02"
  Bytes 9-11:  3-byte LE = number of directory entries
  Bytes 13-15: 3-byte LE = filename table size
  Offset 16+:  Directory entries (16 bytes each)
  After dir:   1 null separator byte
  Then:        Filename table (concatenated filenames, no separators)
  Then:        Data section

Directory entry (16 bytes):
  byte[0]:     0x00 padding
  byte[1-3]:   3-byte LE = offset into filename table (cumulative)
  byte[4]:     0x00 padding
  byte[5]:     filename length (1 byte)
  byte[6-8]:   0x00 0x00 0x00 padding
  byte[9-12]:  4-byte LE = absolute data offset in file
  byte[13-14]: 2-byte LE = file data size (bytes)
  byte[15]:    0x00 padding

Each entry's data is wrapped in a 13-byte IDICOMP header:
  bytes 0-8:  Magic "\x01IDICOMP\x01"
  bytes 9-10: 2-byte LE = compressed payload length
  bytes 11-12: 2-byte LE = type flags (0=raw, 4=IDICOMP compressed)
  bytes 13+:  payload (raw or IDICOMP-compressed data)

IDICOMP compression (type=4): reverse-engineered from TSBASP.dll.
Bit-oriented LZSS variant: 16-bit control words, bit=0 literal, bit=1 encoded.
Encoded ops: run-length fill (types 0,1) or sliding-window copy (types 2-15).
"""

import struct
import os
import sys
import argparse

MAGIC = b'BAY POD\x02'
POD_BAY_MAGIC = b'POD BAY\x01'
IDICOMP_MAGIC = b'\x01IDICOMP\x01'
IDICOMP_HDR = 13  # total header: 9 magic + 2 length + 2 type


def _decompress_idicomp(data):
    """Decompress IDICOMP type-4 payload.

    Input: bytes 11+ of IDICOMP file (type_flags word is the first ctrl_word).
    Reverse-engineered from TSBASP.dll function at file offset 0x35fe9.
    """
    src = 0
    n = len(data)
    out = bytearray()
    mask = 0
    ctrl = 0

    while src < n:
        mask = (mask >> 1) & 0xFFFF
        if mask == 0:
            if src + 2 > n:
                break
            ctrl = data[src] | (data[src + 1] << 8)
            src += 2
            mask = 0x8000

        if (ctrl & mask) == 0:
            if src >= n:
                break
            out.append(data[src])
            src += 1
        else:
            if src >= n:
                break
            b = data[src]; src += 1
            t = (b >> 4) & 0xF   # high nibble = type
            m = b & 0xF           # low nibble

            if t == 0:            # short run: repeat single byte
                ln = m + 3
                if src >= n: break
                fb = data[src]; src += 1
                out.extend([fb] * ln)

            elif t == 1:          # long run: repeat single byte
                if src >= n: break
                nb = data[src]; src += 1
                ln = m + (nb << 4) + 19
                if src >= n: break
                fb = data[src]; src += 1
                out.extend([fb] * ln)

            elif t == 2:          # back-ref, explicit copy length
                if src >= n: break
                nb = data[src]; src += 1
                dist = m + (nb << 4) + 3
                if src >= n: break
                cl = data[src] + 0x10; src += 1
                p = len(out) - dist
                for i in range(cl):
                    out.append(out[p + i])

            else:                 # back-ref, copy length = type nibble (3-15)
                if src >= n: break
                nb = data[src]; src += 1
                dist = m + (nb << 4) + 3
                p = len(out) - dist
                for i in range(t):
                    out.append(out[p + i])

    return bytes(out)


def parse_idicomp(raw):
    """Parse an IDICOMP-wrapped entry and return the decompressed payload.

    Returns (payload_bytes, type_flags) or (None, None) if not IDICOMP.

    Format: 9-byte magic, then one or more compressed chunks. Each chunk is
    a 2-byte LE length followed by that many bytes of LZSS-compressed data.
    Each chunk decompresses to at most 16384 (0x4000) bytes. A zero-length
    chunk or end-of-data terminates the sequence.

    For entries storing raw data (GIF, PDF, etc.) the first chunk's length
    field contains the file's own magic bytes, causing the decompressor to
    raise IndexError; in that case we fall back to returning raw[11:].
    """
    if len(raw) < 11 or raw[:9] != IDICOMP_MAGIC:
        return None, None

    type_flags = (raw[11] + raw[12] * 256) if len(raw) >= 13 else 0

    pos = 9
    total_out = bytearray()

    while pos + 1 < len(raw):
        pl = raw[pos] + raw[pos + 1] * 256
        pos += 2
        if pl == 0:
            break
        chunk = raw[pos:pos + pl]
        try:
            total_out.extend(_decompress_idicomp(chunk))
        except IndexError:
            if not total_out:
                # First chunk failed: not LZSS-compressed, return raw data
                return raw[11:], type_flags
            break
        pos += pl

    if not total_out:
        return None, None
    return bytes(total_out), type_flags


def _detect_ext(payload):
    """Guess file extension from first bytes of payload."""
    if payload[:6] in (b'GIF89a', b'GIF87a'):
        return '.gif'
    if payload[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if payload[:2] == b'\xff\xd8':
        return '.jpg'
    if payload[:4] == b'%PDF':
        return '.pdf'
    if payload[:5].lower() == b'<html':
        return '.htm'
    if payload[:4].lower() == b'<svg':
        return '.svg'
    if payload[:11].lower() in (b'<workunit>\r', b'<workunit>\n') or payload[:10].lower() == b'<workunit>':
        return '.xml'
    if payload[:2] == b'; ':
        return '.wcf'
    # CSS: starts with typical CSS selectors
    if payload[:2] in (b'A ', b'A{', b'BO', b'/*', b'.p', b'.P') and b'{' in payload[:50]:
        return '.css'
    if payload[:5].lower() in (b'body ', b'body{', b'a {co', b'a{col'):
        return '.css'
    return '.bin'


def parse_pod_bay(filepath):
    """Parse a POD BAY .arc file by scanning for sequential IDICOMP blocks.

    POD BAY stores multiple files as concatenated IDICOMP blocks with no
    explicit filename table. Filenames are inferred from the workunit XML
    block (which contains <filename>) and from content type detection.

    Returns (data, entries) in the same format as parse_arc.
    """
    with open(filepath, 'rb') as f:
        data = f.read()

    if not data.startswith(POD_BAY_MAGIC):
        raise ValueError(f"Not a POD BAY file: {filepath}")

    arc_base = os.path.splitext(os.path.basename(filepath))[0].lower()

    # Find all IDICOMP block positions by scanning for the magic
    positions = []
    pos = 0
    while True:
        p = data.find(IDICOMP_MAGIC, pos)
        if p == -1:
            break
        positions.append(p)
        pos = p + 1

    if not positions:
        return data, []

    # First pass: decompress everything to find workunit filename
    main_name = None
    blocks = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(data)
        raw = data[start:end]
        try:
            payload, tf = parse_idicomp(raw)
        except Exception:
            payload = raw[11:] if len(raw) > 11 else raw
        blocks.append(payload)
        # Check for workunit XML with <filename> tag
        if payload and payload[:10].lower().startswith(b'<workunit>'):
            import re
            m = re.search(rb'<filename>([^<]+)</filename>', payload, re.IGNORECASE)
            if m:
                main_name = m.group(1).decode('ascii', errors='replace').strip()

    # Second pass: assign filenames
    entries = []
    ext_counters = {}
    html_idx = 0
    html_files = [b for b in blocks if b and b[:5].lower() == b'<html']
    # The last (largest) HTML is likely the main content page
    main_html_payload = max(html_files, key=len) if html_files else None

    for i, (start, payload) in enumerate(zip(positions, blocks)):
        end = positions[i + 1] if i + 1 < len(positions) else len(data)
        raw_size = end - start

        if payload is None or len(payload) == 0:
            continue

        ext = _detect_ext(payload)

        # Skip workunit XML blocks (metadata only)
        if ext == '.xml':
            continue

        # Assign filename
        if ext == '.htm':
            html_idx += 1
            if main_name and payload is main_html_payload:
                filename = main_name
            elif main_name and len(html_files) == 1:
                filename = main_name
            else:
                stem = os.path.splitext(main_name)[0] if main_name else arc_base
                filename = f'{stem}_{html_idx}.htm'
        elif ext == '.wcf':
            filename = f'{arc_base}.wcf'
        elif ext == '.css':
            stem = os.path.splitext(main_name)[0] if main_name else arc_base
            filename = f'{stem}s.htm'
        elif ext in ('.gif', '.jpg', '.png'):
            c = ext_counters.get(ext, 0) + 1
            ext_counters[ext] = c
            filename = f'{arc_base}_{c}{ext}'
        else:
            c = ext_counters.get(ext, 0) + 1
            ext_counters[ext] = c
            filename = f'{arc_base}_{c}{ext}'

        entries.append({
            'index': i,
            'filename': filename,
            'abs_data_off': start,
            'file_size': raw_size,
        })

    return data, entries


def parse_arc(filepath):
    with open(filepath, 'rb') as f:
        data = f.read()

    if not data.startswith(MAGIC):
        raise ValueError(f"Not a BAY POD file: {filepath}")

    num_entries = data[9] + data[10] * 256 + data[11] * 65536
    fname_table_size = data[13] + data[14] * 256 + data[15] * 65536

    dir_start = 16
    dir_end = dir_start + num_entries * 16
    fname_table_start = dir_end + 1  # +1 for null separator

    entries = []
    for i in range(num_entries):
        entry_off = dir_start + i * 16
        entry = data[entry_off:entry_off + 16]

        name_offset = entry[1] + entry[2] * 256 + entry[3] * 65536
        name_len = entry[5]
        abs_data_off = (entry[9] + entry[10] * 256 +
                        entry[11] * 65536 + entry[12] * 16777216)
        file_size = entry[13] + entry[14] * 256

        try:
            filename = data[
                fname_table_start + name_offset:
                fname_table_start + name_offset + name_len
            ].decode('ascii')
        except Exception:
            filename = f'file_{i:05d}'

        entries.append({
            'index': i,
            'filename': filename,
            'abs_data_off': abs_data_off,
            'file_size': file_size,
        })

    # The file_size field is only 2 bytes (max 65535).  Files larger than
    # 65535 bytes have their size stored mod 65536.  Detect this by sorting
    # entries by data offset and computing the actual block size as the gap
    # to the next block; if the gap exceeds the reported size by an exact
    # multiple of 65536, correct file_size accordingly.
    by_offset = sorted(
        [e for e in entries if e['abs_data_off'] > 0],
        key=lambda e: e['abs_data_off'],
    )
    for i, e in enumerate(by_offset):
        next_off = by_offset[i + 1]['abs_data_off'] if i + 1 < len(by_offset) else len(data)
        gap = next_off - e['abs_data_off']
        if gap > e['file_size'] and (gap - e['file_size']) % 65536 == 0:
            e['file_size'] = gap

    return data, entries


def extract_entry(data, entry, output_dir):
    filename = entry['filename']
    abs_off = entry['abs_data_off']
    size = entry['file_size']

    if abs_off == 0 or size == 0:
        return False, 0

    raw = data[abs_off:abs_off + size]
    if not raw:
        return False, 0

    payload, type_flags = parse_idicomp(raw)

    if payload is None:
        # No IDICOMP wrapper — write raw bytes
        file_data = raw
    else:
        file_data = payload

    if not file_data:
        return False, 0

    out_path = os.path.join(output_dir, filename)
    with open(out_path, 'wb') as f:
        f.write(file_data)
    return True, len(file_data)


def extract_arc(filepath, output_dir, verbose=False, extensions=None):
    print(f"\nExtracting: {os.path.basename(filepath)}")

    try:
        with open(filepath, 'rb') as f:
            magic = f.read(8)
        if magic == POD_BAY_MAGIC:
            data, entries = parse_pod_bay(filepath)
        elif magic.startswith(MAGIC):
            data, entries = parse_arc(filepath)
        else:
            print(f"  SKIP: unknown format {magic[:8].hex()}")
            return 0, 0, 0
    except Exception as e:
        print(f"  ERROR: {e}")
        return 0, 0, 0

    os.makedirs(output_dir, exist_ok=True)

    extracted = 0
    skipped = 0
    total_bytes = 0

    for entry in entries:
        filename = entry['filename']
        ext = os.path.splitext(filename)[1].lower()

        if extensions and ext not in extensions:
            skipped += 1
            continue

        success, nbytes = extract_entry(data, entry, output_dir)
        if success:
            extracted += 1
            total_bytes += nbytes
            if verbose:
                print(f"  OK: {filename} ({nbytes} bytes)")
        else:
            skipped += 1
            if verbose:
                print(f"  SKIP: {filename} (empty or invalid offset)")

    print(f"  Extracted: {extracted} files ({total_bytes:,} bytes), Skipped: {skipped}")
    return extracted, skipped, total_bytes


def list_arc(filepath, extensions=None):
    """List contents of an arc file without extracting."""
    try:
        with open(filepath, 'rb') as f:
            magic = f.read(8)
        if magic == POD_BAY_MAGIC:
            data, entries = parse_pod_bay(filepath)
            fmt = 'POD BAY'
        else:
            data, entries = parse_arc(filepath)
            fmt = 'BAY POD'
    except Exception as e:
        print(f"ERROR: {e}")
        return

    print(f"{fmt} archive: {filepath}")
    print(f"  Entries: {len(entries)}")
    by_ext = {}
    for e in entries:
        ext = os.path.splitext(e['filename'])[1].lower()
        by_ext.setdefault(ext, []).append(e)

    print("  Contents by type:")
    for ext in sorted(by_ext):
        print(f"    {ext or '(no ext)'}: {len(by_ext[ext])} files")

    if extensions:
        print(f"\nFiles matching {extensions}:")
        for e in entries:
            ext = os.path.splitext(e['filename'])[1].lower()
            if ext in extensions:
                print(f"  {e['filename']:40s} {e['file_size']:8d} bytes  @{e['abs_data_off']}")


def main():
    parser = argparse.ArgumentParser(
        description='Extract files from Ford Workshop Manual BAY POD .arc files'
    )
    parser.add_argument('input', nargs='+', help='Input .arc file(s) or directory containing .arc files')
    parser.add_argument('-o', '--output', default='extracted', help='Output directory (default: extracted)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose file-by-file output')
    parser.add_argument('-l', '--list', action='store_true', help='List contents without extracting')
    parser.add_argument('-e', '--ext', action='append',
                        help='Only extract files with this extension (e.g. -e .pdf -e .jpg). '
                             'Default: extract all.')
    args = parser.parse_args()

    extensions = None
    if args.ext:
        extensions = set(e.lower() if e.startswith('.') else '.' + e.lower() for e in args.ext)

    arc_files = []
    for path in args.input:
        if os.path.isdir(path):
            for f in sorted(os.listdir(path)):
                if f.lower().endswith('.arc'):
                    arc_files.append(os.path.join(path, f))
        elif os.path.isfile(path):
            arc_files.append(path)

    if not arc_files:
        print("No .arc files found.")
        return

    if args.list:
        for arc_path in arc_files:
            list_arc(arc_path, extensions)
        return

    total_extracted = 0
    total_skipped = 0
    total_bytes = 0

    for arc_path in arc_files:
        arc_name = os.path.splitext(os.path.basename(arc_path))[0]
        out_dir = os.path.join(args.output, arc_name)
        e, s, b = extract_arc(arc_path, out_dir, verbose=args.verbose, extensions=extensions)
        total_extracted += e
        total_skipped += s
        total_bytes += b

    print(f"\nSummary: {total_extracted:,} files extracted ({total_bytes:,} bytes), "
          f"{total_skipped:,} skipped")
    if not extensions:
        print("\nNote: HTML/SVG/XML files are IDICOMP-decompressed automatically.")


if __name__ == '__main__':
    main()

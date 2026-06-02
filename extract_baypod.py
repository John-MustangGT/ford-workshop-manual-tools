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
import hashlib
import csv
import urllib.request
import urllib.error

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


def _repair_raw_16k_stream(payload):
    """Repair raw payloads containing 2-byte markers every 16KB.

    Certain raw IDICOMP entries are stored as:
      [16KB data][2-byte marker][16KB data][2-byte marker]... [tail][0x0000]
    where marker bytes are framing, not file content.
    """
    block = 0x4000
    if len(payload) <= block + 2 or payload[-2:] != b'\x00\x00':
        return payload

    out = bytearray()
    pos = 0
    markers_seen = 0

    while pos < len(payload):
        take = min(block, len(payload) - pos)
        out.extend(payload[pos:pos + take])
        pos += take

        if take < block:
            break
        if pos + 2 > len(payload):
            return payload

        marker = payload[pos:pos + 2]
        pos += 2
        markers_seen += 1
        if marker == b'\x00\x00':
            break

    if markers_seen == 0:
        return payload
    if len(out) >= 2 and out[-2:] == b'\x00\x00':
        out = out[:-2]
    return bytes(out)


def _repair_raw_16k_stream_variant(payload, marker_bytes=2, require_tail=False, trim_tail=False):
    """Repair a raw stream by dropping marker bytes at each 16KB boundary.

    This is a diagnostic variant used for candidate extraction. It can run
    without requiring a trailing 0x0000 marker and supports dropping 2 or 4
    bytes per 16KB boundary.
    """
    block = 0x4000
    if marker_bytes <= 0:
        return payload
    if len(payload) <= block + marker_bytes:
        return payload
    if require_tail and payload[-2:] != b'\x00\x00':
        return payload

    out = bytearray()
    pos = 0
    markers_seen = 0

    while pos < len(payload):
        take = min(block, len(payload) - pos)
        out.extend(payload[pos:pos + take])
        pos += take

        if take < block:
            break
        if pos + marker_bytes > len(payload):
            break

        pos += marker_bytes
        markers_seen += 1

    if markers_seen == 0:
        return payload
    if trim_tail and out[-2:] == b'\x00\x00':
        out = out[:-2]
    return bytes(out)


def _detect_pending_raw_marker_bytes(payload):
    """Detect whether the framing separator in a pending_raw blob is 2 or 4 bytes.

    When a mixed IDICOMP entry has raw chunks longer than one 16KB block, the
    raw blob contains framing separator bytes at every 16KB position.  Most
    entries use a 2-byte separator, but some use a 4-byte separator (the 2-byte
    separator followed by an additional 2-byte \x00\x00 field).  Detect the
    latter by checking whether the two bytes *immediately after* the first
    separator position are \x00\x00.
    """
    block = 0x4000
    if len(payload) <= block + 4:
        return 2
    return 4 if payload[block + 2:block + 4] == b'\x00\x00' else 2


def parse_idicomp(raw):
    """Parse an IDICOMP-wrapped entry and return the decompressed payload.

    Returns (payload_bytes, type_flags) or (None, None) if not IDICOMP.

    Format: 9-byte magic, then one or more chunks. Each chunk is a 2-byte LE
    signed integer followed by data bytes. A zero chunk header terminates the
    sequence. A positive header is the LZSS compressed data size; a negative
    header encodes a raw (uncompressed) chunk of abs(header) bytes.

    This matches the original C implementation (FUN_0041b0a1 in tsobrowser.exe).
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
        pl_signed = pl if pl < 0x8000 else pl - 0x10000
        if pl_signed < 0:
            # Raw chunk: abs(signed value) bytes of uncompressed data.
            size = -pl_signed
            total_out.extend(raw[pos:pos + size])
            pos += size
        else:
            # LZSS chunk: decompress and append.
            chunk = raw[pos:pos + pl]
            total_out.extend(_decompress_idicomp(chunk))
            pos += pl

    if not total_out:
        return None, None

    return bytes(total_out), type_flags


def _parse_idicomp_mixed_raw(raw, include_first_raw_hdr=False, repair_func=None):
    """Parse IDICOMP with tunable mixed-raw handling for diagnostics."""
    if len(raw) < 11 or raw[:9] != IDICOMP_MAGIC:
        return None, None

    type_flags = (raw[11] + raw[12] * 256) if len(raw) >= 13 else 0

    pos = 9
    total_out = bytearray()
    pending_raw = None

    while pos + 1 < len(raw):
        hdr = raw[pos:pos + 2]
        pl = raw[pos] + raw[pos + 1] * 256
        pos += 2
        if pl == 0:
            break

        chunk = raw[pos:pos + pl]
        try:
            decompressed = _decompress_idicomp(chunk)
            if pending_raw is not None:
                if repair_func is None:
                    total_out.extend(bytes(pending_raw))
                else:
                    total_out.extend(repair_func(bytes(pending_raw)))
                pending_raw = None
            total_out.extend(decompressed)
        except IndexError:
            if not total_out:
                # All-raw entry: apply the caller-supplied repair_func so that
                # candidate variants are actually distinct for these entries.
                repair = repair_func if repair_func is not None else _repair_raw_16k_stream
                return repair(raw[11:]), type_flags
            if pending_raw is None:
                pending_raw = bytearray()
                if include_first_raw_hdr:
                    pending_raw.extend(hdr)
                pending_raw.extend(chunk)
            else:
                pending_raw.extend(hdr)
                pending_raw.extend(chunk)
        pos += pl

    if pending_raw is not None:
        if repair_func is None:
            total_out.extend(bytes(pending_raw))
        else:
            total_out.extend(repair_func(bytes(pending_raw)))

    if not total_out:
        return None, None

    return bytes(total_out), type_flags


def _candidate_payloads_from_raw(raw):
    """Build de-duplicated decode candidates for one raw IDICOMP entry."""
    candidates = []

    default_payload, _ = parse_idicomp(raw)
    if default_payload is None:
        default_payload = raw
    candidates.append(('default', default_payload))

    variants = [
        (
            'mixed_no_repair',
            False,
            None,
        ),
        (
            'mixed_drop2_relaxed',
            False,
            lambda b: _repair_raw_16k_stream_variant(b, marker_bytes=2, require_tail=False, trim_tail=False),
        ),
        (
            'mixed_drop2_relaxed_trim',
            False,
            lambda b: _repair_raw_16k_stream_variant(b, marker_bytes=2, require_tail=False, trim_tail=True),
        ),
        (
            'mixed_drop4_relaxed',
            False,
            lambda b: _repair_raw_16k_stream_variant(b, marker_bytes=4, require_tail=False, trim_tail=False),
        ),
        (
            'mixed_drop4_relaxed_trim',
            False,
            lambda b: _repair_raw_16k_stream_variant(b, marker_bytes=4, require_tail=False, trim_tail=True),
        ),
        (
            'mixed_with_hdr_drop2_relaxed',
            True,
            lambda b: _repair_raw_16k_stream_variant(b, marker_bytes=2, require_tail=False, trim_tail=False),
        ),
        (
            'mixed_with_hdr_no_repair',
            True,
            None,
        ),
    ]

    for name, include_hdr, repair_func in variants:
        payload, _ = _parse_idicomp_mixed_raw(raw, include_first_raw_hdr=include_hdr, repair_func=repair_func)
        if payload:
            candidates.append((name, payload))

    uniq = []
    seen = set()
    for variant, payload in candidates:
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        uniq.append((variant, payload, digest))
    return uniq


def extract_entry_candidates(data, entry, output_dir, verbose=False):
    """Write multiple decode candidates for one entry to help diagnose issues."""
    filename = entry['filename']
    abs_off = entry['abs_data_off']
    size = entry['file_size']

    if abs_off == 0 or size == 0:
        return []

    raw = data[abs_off:abs_off + size]
    if not raw:
        return []

    uniq = _candidate_payloads_from_raw(raw)

    os.makedirs(output_dir, exist_ok=True)
    stem, ext = os.path.splitext(filename)
    written = []
    for variant, payload, digest in uniq:
        out_name = f'{stem}__{variant}{ext}'
        out_path = os.path.join(output_dir, out_name)
        with open(out_path, 'wb') as f:
            f.write(payload)
        written.append((variant, out_path, len(payload), digest))
        if verbose:
            print(f'  CANDIDATE: {os.path.basename(out_path)} ({len(payload)} bytes) sha256={digest}')

    return written


def probe_file_candidates(arc_files, target_name, output_dir, verbose=False):
    """Extract candidate decodes for one file name from matching arc entries."""
    target_lower = target_name.lower()
    total_written = 0
    matched_any = False

    for arc_path in arc_files:
        try:
            with open(arc_path, 'rb') as f:
                magic = f.read(8)
            if magic == POD_BAY_MAGIC:
                data, entries = parse_pod_bay(arc_path)
            elif magic.startswith(MAGIC):
                data, entries = parse_arc(arc_path)
            else:
                continue
        except Exception as e:
            print(f'  ERROR reading {arc_path}: {e}')
            continue

        arc_name = os.path.splitext(os.path.basename(arc_path))[0]
        for entry in entries:
            if entry['filename'].lower() != target_lower:
                continue
            matched_any = True
            print(f'\nProbing {entry["filename"]} in {arc_name}.arc')
            arc_out = os.path.join(output_dir, arc_name)
            written = extract_entry_candidates(data, entry, arc_out, verbose=verbose)
            if not written:
                print('  No candidates produced')
                continue
            total_written += len(written)
            for variant, out_path, nbytes, digest in written:
                print(f'  {variant:28s} {nbytes:8d} bytes  sha256={digest}')

    if not matched_any:
        print(f'No matching file found for: {target_name}')
        return 1

    print(f'\nCandidate probe complete: {total_written} files written under {output_dir}')
    return 0


def _score_candidate(payload, remote):
    m = min(len(payload), len(remote))
    first_diff = None
    for i in range(m):
        if payload[i] != remote[i]:
            first_diff = i
            break
    prefix_match = m if first_diff is None else first_diff
    exact_match = payload == remote
    return prefix_match, first_diff, exact_match


def probe_score_csv(arc_files, report_csv, output_csv, limit=None):
    """Score candidate decodes for mismatches listed in a prior report CSV."""
    try:
        with open(report_csv, newline='') as f:
            rows = [r for r in csv.DictReader(f) if r.get('status') == 'mismatch']
    except Exception as e:
        print(f'ERROR reading report CSV: {e}')
        return 1

    if limit is not None:
        rows = rows[:limit]
    if not rows:
        print('No mismatch rows found in report CSV.')
        return 1

    target_names = {r['filename'].lower() for r in rows if r.get('filename')}

    # Build index for needed entries across provided arc files.
    entry_index = {}
    for arc_path in arc_files:
        try:
            with open(arc_path, 'rb') as f:
                magic = f.read(8)
            if magic == POD_BAY_MAGIC:
                data, entries = parse_pod_bay(arc_path)
            elif magic.startswith(MAGIC):
                data, entries = parse_arc(arc_path)
            else:
                continue
        except Exception as e:
            print(f'  ERROR reading {arc_path}: {e}')
            continue

        arc_name = os.path.splitext(os.path.basename(arc_path))[0]
        for entry in entries:
            key = entry['filename'].lower()
            if key not in target_names:
                continue
            if key not in entry_index:
                entry_index[key] = (arc_name, data, entry)

    out_rows = []
    processed = 0
    for r in rows:
        name = r.get('filename', '')
        key = name.lower()
        remote_url = r.get('remote_url', '')
        if key not in entry_index:
            out_rows.append({
                'filename': name,
                'status': 'missing_local_entry',
                'best_variant': '',
                'best_prefix_match': '',
                'best_first_diff': '',
                'best_len': '',
                'best_sha256': '',
                'remote_len': '',
                'remote_sha256': '',
                'arc': '',
                'detail': 'filename not found in provided arc inputs',
            })
            continue

        arc_name, data, entry = entry_index[key]
        raw = data[entry['abs_data_off']:entry['abs_data_off'] + entry['file_size']]

        try:
            req = urllib.request.Request(remote_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                remote = resp.read()
        except Exception as e:
            out_rows.append({
                'filename': name,
                'status': 'remote_error',
                'best_variant': '',
                'best_prefix_match': '',
                'best_first_diff': '',
                'best_len': '',
                'best_sha256': '',
                'remote_len': '',
                'remote_sha256': '',
                'arc': arc_name,
                'detail': str(e),
            })
            continue

        remote_sha = hashlib.sha256(remote).hexdigest()
        candidates = _candidate_payloads_from_raw(raw)
        if not candidates:
            out_rows.append({
                'filename': name,
                'status': 'no_candidates',
                'best_variant': '',
                'best_prefix_match': '',
                'best_first_diff': '',
                'best_len': '',
                'best_sha256': '',
                'remote_len': len(remote),
                'remote_sha256': remote_sha,
                'arc': arc_name,
                'detail': '',
            })
            continue

        scored = []
        for variant, payload, digest in candidates:
            prefix_match, first_diff, exact_match = _score_candidate(payload, remote)
            scored.append((prefix_match, exact_match, variant, first_diff, len(payload), digest))

        # Highest matching prefix wins; exact match breaks ties, then longer file.
        scored.sort(key=lambda x: (x[0], 1 if x[1] else 0, x[4]), reverse=True)
        best = scored[0]
        status = 'exact_match' if best[1] else 'best_candidate'

        out_rows.append({
            'filename': name,
            'status': status,
            'best_variant': best[2],
            'best_prefix_match': best[0],
            'best_first_diff': '' if best[3] is None else best[3],
            'best_len': best[4],
            'best_sha256': best[5],
            'remote_len': len(remote),
            'remote_sha256': remote_sha,
            'arc': arc_name,
            'detail': '',
        })

        processed += 1
        if processed % 10 == 0:
            print(f'  scored {processed}/{len(rows)} mismatches...')

    os.makedirs(os.path.dirname(output_csv) or '.', exist_ok=True)
    fieldnames = [
        'filename', 'status', 'best_variant', 'best_prefix_match', 'best_first_diff',
        'best_len', 'best_sha256', 'remote_len', 'remote_sha256', 'arc', 'detail'
    ]
    with open(output_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    exact = sum(1 for r in out_rows if r['status'] == 'exact_match')
    best_only = sum(1 for r in out_rows if r['status'] == 'best_candidate')
    errs = sum(1 for r in out_rows if r['status'] in ('remote_error', 'missing_local_entry', 'no_candidates'))
    print(f'\nProbe scoring complete: exact={exact}, best_candidate={best_only}, errors={errs}')
    print(f'Wrote: {output_csv}')
    return 0


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
    parser.add_argument('--probe-file',
                        help='Generate decode candidates for one file name (case-insensitive).')
    parser.add_argument('--probe-output', default='tmp/candidates',
                        help='Output directory for --probe-file candidates (default: tmp/candidates).')
    parser.add_argument('--probe-score-csv',
                        help='Score candidate variants for mismatch rows from a report CSV.')
    parser.add_argument('--probe-score-output', default='tmp/candidates/probe_score_results.csv',
                        help='Output CSV path for --probe-score-csv results.')
    parser.add_argument('--probe-score-limit', type=int,
                        help='Optional maximum number of mismatch rows to score.')
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

    if args.probe_file:
        sys.exit(probe_file_candidates(
            arc_files,
            args.probe_file,
            args.probe_output,
            verbose=args.verbose,
        ))

    if args.probe_score_csv:
        sys.exit(probe_score_csv(
            arc_files,
            args.probe_score_csv,
            args.probe_score_output,
            limit=args.probe_score_limit,
        ))

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

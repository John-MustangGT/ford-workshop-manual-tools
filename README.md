# Ford Workshop Manual Arc Extractor

Extracts files from Ford Workshop Manual `.arc` container files without requiring Ford's proprietary software. Handles both container formats (`BAY POD` and `POD BAY`) and decompresses all IDICOMP-compressed entries.

## Usage

```
python3 extract_baypod.py <input> [<input> ...] [-o <output_dir>] [-v] [-l] [-e <ext>]
```

**Arguments:**

| Flag | Description |
|------|-------------|
| `input` | One or more `.arc` files, or a directory containing `.arc` files |
| `-o DIR` | Output directory (default: `extracted`) |
| `-v` | Verbose: print each extracted filename |
| `-l` | List contents without extracting |
| `-e EXT` | Only extract files with this extension (repeatable, e.g. `-e .htm -e .svg`) |

**Examples:**

```bash
# Extract everything from a directory of arc files
python3 extract_baypod.py content/useni4 -o extracted

# List contents of a single arc
python3 extract_baypod.py content/useni4/SEB.arc -l

# Extract only HTML and SVG files
python3 extract_baypod.py content/useni4 -o extracted -e .htm -e .HTM -e .svg
```

Each `.arc` file extracts into its own subdirectory named after the arc (e.g. `extracted/SEB/`).

---

## Format Reference

### BAY POD Container (`.arc`)

Magic: `BAY POD\x02` (8 bytes)

```
Offset  Size  Description
------  ----  -----------
0       8     Magic: "BAY POD\x02"
8       1     Padding (0x00)
9       3     Number of directory entries (3-byte LE)
12      1     Padding (0x00)
13      3     Filename table size in bytes (3-byte LE)
16      N*16  Directory entries (16 bytes each)
16+N*16 1     Null separator byte
...     ?     Filename table (concatenated filenames, no separators)
...     ?     Data section (IDICOMP-wrapped file data)
```

**Directory entry (16 bytes):**

```
Byte(s)  Description
-------  -----------
0        Padding (0x00)
1–3      Offset into filename table (3-byte LE)
4        Padding (0x00)
5        Filename length (1 byte)
6–8      Padding (0x00 0x00 0x00)
9–12     Absolute data offset in file (4-byte LE)
13–14    Size of IDICOMP-wrapped data block (2-byte LE)
15       Padding (0x00)
```

### POD BAY Container (`.arc`)

Magic: `POD BAY\x01` (8 bytes)

Used for recall/TSB documents. Has the same header layout as BAY POD but contains no usable filename table — filenames are inferred from a workunit XML block embedded in the data. The data section is a sequence of IDICOMP-wrapped blocks stored end-to-end with no directory; the extractor scans for `\x01IDICOMP\x01` magic to locate each block.

The workunit block contains `<filename>r98s20.htm</filename>` which gives the primary output filename. Other blocks are assigned names derived from the archive basename and detected content type.

---

## IDICOMP Format

Every data block in the arc (whether in BAY POD or POD BAY) is wrapped in an IDICOMP header. The `\x01IDICOMP\x01` magic appears in the data section at the absolute offset given by the directory entry.

**IDICOMP block layout:**

```
Offset  Size  Description
------  ----  -----------
0       9     Magic: "\x01IDICOMP\x01"
9       2     Compressed length of first chunk (2-byte LE)
11      ?     First compressed chunk (see below)
...     ...   Additional chunks (see Multi-chunk blocks)
```

The payload begins immediately at byte 9. There is no separate "type flags" field — what was originally documented as such is actually the first two bytes of the first compressed chunk (the first LZSS control word).

**Raw (uncompressed) files:**

For binary files (JPEG, GIF, PDF, etc.) the bytes at offset 9–10 contain the first two bytes of the actual file data rather than a valid LZSS length. The decompressor detects this because the first chunk produces an out-of-bounds back-reference (`IndexError`); in that case the raw bytes starting at offset 11 are returned directly.

Common signatures seen at offset 11:
- `FF D8` → JPEG (stored raw from byte 11)
- `47 49` (`GI…`) → GIF (stored raw from byte 11)
- `25 50` (`%P…`) → PDF (stored raw from byte 11)
- `3B 20` (`;` ` `) → WCF text (stored raw from byte 11)

---

## IDICOMP Multi-chunk Blocks

Large files are stored as a sequence of independently compressed chunks within a single IDICOMP block. Each chunk decompresses to at most **16,384 bytes** (0x4000). The format after the 9-byte magic is:

```
[2-byte LE chunk_len] [chunk_len bytes of LZSS data]
[2-byte LE chunk_len] [chunk_len bytes of LZSS data]
...
[0x00 0x00]   ← terminator (zero-length chunk)
```

The decompressor concatenates the output of all chunks to reconstruct the complete file. Single-chunk files (≤16 KB decompressed) have exactly one chunk followed by the terminator.

---

## IDICOMP Decompression Algorithm

Reverse-engineered from `TSBASP.dll` at file offset `0x35fe9`. Source project identified via debug symbols as `D:\develop\common\idicomm2\DB.CPP`.

The algorithm is a **bit-oriented LZSS variant** with 16-bit control words.

### Control word processing

```
mask  = 0
ctrl  = 0
src   = 0  (index into compressed input)
out   = [] (output buffer)

loop:
    mask >>= 1
    if mask == 0:
        ctrl = read_u16_le()   # reload control word
        mask = 0x8000

    if (ctrl & mask) == 0:
        out.append(read_byte())        # literal
    else:
        decode_encoded_op()            # back-reference or run-fill
```

Control words are 16-bit little-endian values. Bits are tested MSB-first (mask starts at `0x8000` and shifts right). A `0` bit means the next byte is a literal; a `1` bit means an encoded operation follows.

**The first control word is also the first two bytes of each chunk** (bytes 11–12 of the IDICOMP block for the first chunk, or the first two bytes of subsequent chunks).

### Encoded operations

When a `1` bit is encountered, one byte `b` is read:

- `t = (b >> 4) & 0xF`  — high nibble, operation type
- `m = b & 0xF`          — low nibble, parameter

| Type (`t`) | Description |
|------------|-------------|
| 0 | **Short run-fill**: read `fill_byte`; emit `(m + 3)` copies |
| 1 | **Long run-fill**: read `nb`, then `fill_byte`; emit `(m + (nb << 4) + 19)` copies |
| 2 | **Back-ref, explicit length**: read `nb`, then `cl`; copy `(cl + 16)` bytes from `out[current - dist]` where `dist = m + (nb << 4) + 3` |
| 3–15 | **Back-ref, implicit length**: read `nb`; copy `t` bytes from `out[current - dist]` where `dist = m + (nb << 4) + 3` |

Back-references use a sliding window over the already-decoded output. Overlapping copies (where `dist < copy_length`) are handled byte-by-byte to correctly implement run-length encoding via back-reference.

### Python implementation

```python
def _decompress_idicomp(data):
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
            t = (b >> 4) & 0xF
            m = b & 0xF

            if t == 0:                        # short run-fill
                ln = m + 3
                fb = data[src]; src += 1
                out.extend([fb] * ln)

            elif t == 1:                      # long run-fill
                nb = data[src]; src += 1
                ln = m + (nb << 4) + 19
                fb = data[src]; src += 1
                out.extend([fb] * ln)

            elif t == 2:                      # back-ref, explicit copy length
                nb = data[src]; src += 1
                dist = m + (nb << 4) + 3
                cl = data[src] + 0x10; src += 1
                p = len(out) - dist
                for i in range(cl):
                    out.append(out[p + i])

            else:                             # back-ref, copy length = type nibble
                nb = data[src]; src += 1
                dist = m + (nb << 4) + 3
                p = len(out) - dist
                for i in range(t):
                    out.append(out[p + i])

    return bytes(out)
```

---

## Corpus Notes (Ford Workshop Manual)

Typical content extracted from a full install:

| Extension | Count | Description |
|-----------|-------|-------------|
| `.HTM`/`.htm` | ~36,000 | Service procedure HTML pages |
| `.SVG` | ~11,000 | Wiring diagram vector graphics |
| `.JPG` | ~80,000 | Procedure step photographs |
| `.GIF` | ~15,000 | Diagrams and icons |
| `.XML`/`.xml` | ~18,000 | Structured data / parts info |
| `.PDF` | ~5,000 | Supplement and spec sheets |
| `.wcf` | ~5,600 | Workunit control files (metadata) |

Named arc files correspond to vehicle lines and model years (e.g. `SEB.arc` = S197 Mustang, `SE2.arc` = wiring diagrams, `VIE.arc` = vintage reference images).

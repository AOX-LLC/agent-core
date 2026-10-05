"""Check media files for anything that could carry a name outside the picture or the sound.

The public-safety hook reads text and skips binary media, because a short term matches the
compressed bytes of a picture or a video by chance. What can carry a name in a media file is
everything that is not pixels or samples, so this allows only known-safe structure and fails on
the rest:

- PNG: the signature, then only the chunks in PNG_CHUNKS, then nothing after IEND.
- GIF: the signature, then only image blocks, graphic-control extensions and a single NETSCAPE2.0
  loop block. A comment, plain-text or other application extension, an unknown byte or anything
  after the trailer fails.
- mp4: no `uuid`, `udta` or `meta` box (top level or inside `moov`) except the empty `udta` ffmpeg
  writes, no text in `free`/`skip`.
- mp4 and webm: no chapters, no attachments, and only the technical tags in ALLOWED_TAGS.
- Anything else with a media suffix (jpg, jpeg, webp) is refused: convert it to png first.

When `.denylist.local` exists (it never enters the repository) the text of every metadata field is
also searched for its terms. A file whose suffix and signature disagree fails.

Needs ffprobe for mp4 and webm. Remove metadata with tools/media/strip_media_metadata.py.
Prints the file and the rule, never the denylisted term.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

DENYLIST = Path(__file__).resolve().parents[2] / ".denylist.local"
ALLOWED_TAGS = {
    "language",
    "handler_name",
    "vendor_id",
    "major_brand",
    "minor_version",
    "compatible_brands",
    "encoder",
    "duration",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHUNKS = {
    b"IHDR",
    b"PLTE",
    b"IDAT",
    b"IEND",
    b"tRNS",
    b"gAMA",
    b"cHRM",
    b"sRGB",
    b"pHYs",
    b"bKGD",
    b"acTL",
    b"fcTL",
    b"fdAT",
}
MP4_FORBIDDEN_BOXES = {b"uuid", b"udta", b"meta"}
# What ffmpeg writes for a file with no metadata: a udta holding an empty meta (an hdlr and an empty
# ilst). It carries no text, so it is the one udta that is allowed.
EMPTY_UDTA = (
    b"\x00\x00\x005meta\x00\x00\x00\x00\x00\x00\x00!hdlr\x00\x00\x00\x00\x00\x00\x00\x00mdir"
    b"appl\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08ilst"
)
MEDIA_SUFFIXES = {".png", ".gif", ".mp4", ".webm"}
REFUSED_SUFFIXES = {".jpg", ".jpeg", ".webp"}


def load_terms() -> list[str]:
    if not DENYLIST.is_file():
        return []
    lines = DENYLIST.read_text(encoding="utf-8").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


def png_findings(data: bytes) -> tuple[list[str], str]:
    if not data.startswith(PNG_SIGNATURE):
        return ["not a PNG (wrong signature)"], ""
    findings, text, position = [], "", len(PNG_SIGNATURE)
    ended = False
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        if ended:
            findings.append("bytes after the PNG IEND chunk")
            text += data[position:].decode("latin-1", errors="ignore")
            break
        if kind not in PNG_CHUNKS:
            findings.append(f"PNG {kind.decode('latin-1')} chunk")
            text += body.decode("latin-1", errors="ignore") + "\n"
        ended = kind == b"IEND"
        position += 12 + length
    if not ended and not findings:
        findings.append("PNG has no IEND chunk")
    return findings, text


def _sub_blocks(data: bytes, position: int) -> tuple[int, list[bytes]]:
    blocks = []
    while data[position] != 0:
        size = data[position]
        blocks.append(data[position + 1 : position + 1 + size])
        position += 1 + size
    return position + 1, blocks


def gif_findings(data: bytes) -> tuple[list[str], str]:
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        return ["not a GIF (wrong signature)"], ""
    findings, text = [], ""
    flags = data[10]
    position = 13 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0)
    while True:
        marker = data[position]
        if marker == 0x3B:  # trailer
            if position + 1 != len(data):
                findings.append("bytes after the GIF trailer")
                text += data[position + 1 :].decode("latin-1", errors="ignore")
            break
        if marker == 0x2C:  # image descriptor, then local colour table, LZW size, data
            flags = data[position + 9]
            position += 10 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0) + 1
            position, _ = _sub_blocks(data, position)
        elif marker == 0x21:  # extension
            label = data[position + 1]
            position, blocks = _sub_blocks(data, position + 2)
            joined = b"".join(blocks).decode("latin-1", errors="ignore")
            if label == 0xF9:  # graphic control: timing only
                continue
            if (
                label == 0xFF
                and len(blocks) == 2
                and blocks[0] == b"NETSCAPE2.0"
                and len(blocks[1]) == 3
            ):
                continue
            kinds = {0xFE: "comment", 0x01: "plain-text", 0xFF: "application"}
            findings.append(f"GIF {kinds.get(label, 'unknown')} extension")
            text += joined + "\n"
        else:
            findings.append("GIF has an unknown block")
            break
    return findings, text


def mp4_boxes(data: bytes, start: int, end: int):  # type: ignore[no-untyped-def]
    position = start
    while position + 8 <= end:
        size, kind = struct.unpack(">I4s", data[position : position + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[position + 8 : position + 16])[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header:
            raise ValueError("bad mp4 box size")
        yield kind, data[position + header : position + size], position + header, position + size
        position += size


def mp4_findings(data: bytes) -> tuple[list[str], str]:
    if data[4:8] != b"ftyp":
        return ["not an mp4 (no ftyp box)"], ""
    findings, text = [], ""
    for kind, body, start, end in mp4_boxes(data, 0, len(data)):
        if kind in MP4_FORBIDDEN_BOXES:
            findings.append(f"mp4 {kind.decode('latin-1')} box")
            text += body.decode("latin-1", errors="ignore")
        elif kind in (b"free", b"skip") and body.strip(b"\x00"):
            findings.append("mp4 free box with content")
            text += body.decode("latin-1", errors="ignore")
        elif kind == b"moov":
            for inner, inner_body, _, _ in mp4_boxes(data, start, end):
                if inner in MP4_FORBIDDEN_BOXES and not (
                    inner == b"udta" and inner_body == EMPTY_UDTA
                ):
                    findings.append(f"mp4 moov/{inner.decode('latin-1')} box")
                    text += inner_body.decode("latin-1", errors="ignore")
    return findings, text


def video_findings(path: Path) -> tuple[list[str], str]:
    data = path.read_bytes()
    if path.suffix.lower() == ".mp4":
        findings, text = mp4_findings(data)
        if findings and findings[0].startswith("not an mp4"):
            return findings, text  # no need to ask ffprobe about a file that is not one
    elif data[:4] != b"\x1a\x45\xdf\xa3":
        return ["not a webm (no EBML header)"], ""
    else:
        findings, text = [], ""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise SystemExit("ffprobe is required to check mp4 and webm metadata")
    probe = subprocess.run(  # noqa: S603 - a fixed program and a path from the commit
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags:chapters:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(probe.stdout or "{}")
    if parsed.get("chapters"):
        findings.append("chapters")
    if any(stream.get("codec_type") == "attachment" for stream in parsed.get("streams", [])):
        findings.append("an attachment stream")
    tag_sets = [parsed.get("format", {}).get("tags", {})]
    tag_sets += [stream.get("tags", {}) for stream in parsed.get("streams", [])]
    for tags in tag_sets:
        for key, value in tags.items():
            text += f"{key}={value}\n"
            if key.lower() not in ALLOWED_TAGS:
                findings.append(f"metadata tag {key!r}")
    return findings, text


def check(path: Path, terms: list[str]) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in REFUSED_SUFFIXES:
        return [f"{suffix} is not supported by this check: convert it to png"]
    if suffix not in MEDIA_SUFFIXES:
        return []
    try:
        if suffix == ".png":
            findings, text = png_findings(path.read_bytes())
        elif suffix == ".gif":
            findings, text = gif_findings(path.read_bytes())
        else:
            findings, text = video_findings(path)
    except (IndexError, struct.error, ValueError, subprocess.CalledProcessError):
        return ["unreadable or malformed: it could not be checked"]
    lowered = text.lower()
    if any(term in lowered for term in terms):
        findings.append("a denylisted term in the metadata")
    return findings


def main(paths: list[str]) -> int:
    terms, failed = load_terms(), False
    for name in paths:
        for finding in check(Path(name), terms):
            failed = True
            print(f"{name}: {finding}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

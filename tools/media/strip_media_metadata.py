"""Strip everything that is not picture or sound from media files, in place, without re-encoding.

    python3 tools/media/strip_media_metadata.py <file>...

PNG: keep the chunks check_media_metadata.py allows, drop the rest and anything after IEND.
GIF: drop comment, plain-text and other application extensions (the NETSCAPE2.0 loop block stays).
mp4 and webm: remux with ffmpeg, copying the streams, with no metadata or chapters (mp4 keeps
faststart). Check the result with check_media_metadata.py.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_media_metadata import PNG_CHUNKS, PNG_SIGNATURE, _sub_blocks


def strip_png(data: bytes) -> bytes:
    out, position = [PNG_SIGNATURE], len(PNG_SIGNATURE)
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        chunk = data[position : position + 12 + length]
        if kind in PNG_CHUNKS:
            out.append(chunk)
        position += 12 + length
        if kind == b"IEND":
            break
    return b"".join(out)


def strip_gif(data: bytes) -> bytes:
    flags = data[10]
    position = 13 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0)
    out = [data[:position]]
    while data[position] != 0x3B:
        start = position
        if data[position] == 0x2C:
            flags = data[position + 9]
            position += 10 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0) + 1
            position, _ = _sub_blocks(data, position)
            out.append(data[start:position])
        else:
            label = data[position + 1]
            position, blocks = _sub_blocks(data, position + 2)
            keep = label == 0xF9 or (
                label == 0xFF and blocks[:1] == [b"NETSCAPE2.0"] and len(blocks) == 2
            )
            if keep:
                out.append(data[start:position])
    out.append(b"\x3b")
    return b"".join(out)


def strip_video(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is required to strip mp4 and webm files")
    temporary = path.with_name(f"{path.stem}.stripping{path.suffix}")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c",
        "copy",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
    ]
    if path.suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
    subprocess.run([*command, str(temporary)], check=True)  # noqa: S603
    temporary.replace(path)


def main(paths: list[str]) -> int:
    for name in paths:
        path = Path(name)
        suffix = path.suffix.lower()
        if suffix == ".png":
            path.write_bytes(strip_png(path.read_bytes()))
        elif suffix == ".gif":
            path.write_bytes(strip_gif(path.read_bytes()))
        elif suffix in (".mp4", ".webm"):
            strip_video(path)
        else:
            print(f"{name}: not a supported media file, left alone", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

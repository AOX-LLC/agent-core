#!/usr/bin/env bash
# Render the approval_flow terminal clip: tools/media/render.sh
# The social preview is tools/media/social-preview.html shot at 1280x640 (chromium --headless --screenshot), then stripped and checked the same way.
# Needs vhs, ttyd (on PATH or in $TOOLS_BIN), ffmpeg and ffprobe. Writes docs/media/approval-flow.{gif,mp4},
# then strips and checks their metadata. The repo path appears only inside the hidden block of the tape body.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
out=$repo/docs/media
clip=approval-flow
export PATH="${TOOLS_BIN:+$TOOLS_BIN:}$PATH"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

{
  echo 'Output "'"$work/$clip.webm"'"'
  echo 'Set Shell "bash"'
  echo 'Set FontFamily "DejaVu Sans Mono"'
  echo 'Set FontSize 17'
  echo 'Set Width 1100'
  echo 'Set Height 760'
  echo 'Set Padding 24'
  echo 'Set Margin 0'
  echo 'Set Framerate 30'
  echo 'Set TypingSpeed 45ms'
  echo 'Set Theme {"name":"agent-core","background":"#0E1012","foreground":"#E6E8EA","cursor":"#3FC1B0","cursorAccent":"#0E1012","selection":"#1C2024","black":"#15181B","brightBlack":"#6B747D","red":"#F2707F","brightRed":"#F58A96","green":"#4CC985","brightGreen":"#6ED79C","yellow":"#E8B23A","brightYellow":"#EEC463","blue":"#7FB2F0","brightBlue":"#9AC4F4","magenta":"#A792F2","brightMagenta":"#BCAAF5","cyan":"#3FC1B0","brightCyan":"#63D0C1","white":"#C3C8CD","brightWhite":"#E6E8EA"}'
  sed "s|@REPO@|$repo|g" "$here/$clip.tape.body"
} > "$work/$clip.tape"

# Run the command once off screen first: abort if its output would put a path or user name in the frame.
dry=$(cd "$repo" && env -u VIRTUAL_ENV UV_NO_PROGRESS=1 PYTHONWARNINGS=ignore uv run --extra otel python examples/approval_flow.py 2>&1)
if grep -q -F -e "$repo" -e "$HOME" -e "$USER" <<<"$dry"; then echo "output contains a path or user name; not recording" >&2; exit 1; fi

(cd "$repo" && vhs "$work/$clip.tape")

ffmpeg -v error -y -i "$work/$clip.webm" -vf "fps=30,format=yuv420p" \
  -c:v libx264 -crf 27 -preset slow -movflags +faststart -an -bsf:v filter_units=remove_types=6 "$out/$clip.mp4"
# GIF: 900 px wide, palette method; halve the frame rate once if it lands over 3 MB.
for fps in 15 10; do
  ffmpeg -v error -y -i "$work/$clip.webm" \
    -vf "fps=$fps,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none" \
    "$out/$clip.gif"
  [ "$(stat -c%s "$out/$clip.gif")" -le 3145728 ] && break
done

python3 "$here/strip_media_metadata.py" "$out/$clip.gif" "$out/$clip.mp4"
python3 "$here/check_media_metadata.py" "$out/$clip.gif" "$out/$clip.mp4"
ls -l "$out/$clip".{mp4,gif}

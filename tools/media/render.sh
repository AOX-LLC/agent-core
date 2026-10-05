#!/usr/bin/env bash
# Render the approval_flow terminal clip: tools/media/render.sh [light|dark]
# No argument writes approval-flow.{gif,mp4} (the README fallback). `light` or `dark` writes only approval-flow-<theme>.gif, for the README <picture>.
# The social preview is tools/media/social-preview.html shot at 1280x640 (chromium --headless --screenshot), then stripped and checked the same way.
# Needs vhs, ttyd (on PATH or in $TOOLS_BIN), ffmpeg and ffprobe. With no argument it writes docs/media/approval-flow.{gif,mp4},
# then strips and checks their metadata. The repo path appears only inside the hidden block of the tape body.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
out=$repo/docs/media
clip=approval-flow
theme=${1:-}
case "$theme" in
  "") out_name=$clip; theme=dark ;;
  light|dark) out_name=$clip-$theme ;;
  *) echo "usage: render.sh [light|dark]" >&2; exit 2 ;;
esac
export PATH="${TOOLS_BIN:+$TOOLS_BIN:}$PATH"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

{
  echo 'Output "'"$work/$out_name.webm"'"'
  echo 'Set Shell "bash"'
  echo 'Set FontFamily "DejaVu Sans Mono"'
  echo 'Set FontSize 17'
  echo 'Set Width 1100'
  echo 'Set Height 760'
  echo 'Set Padding 24'
  echo 'Set Margin 0'
  echo 'Set Framerate 30'
  echo 'Set TypingSpeed 45ms'
  case "$theme" in
    dark) echo 'Set Theme {"name":"agent-core","background":"#0E1012","foreground":"#E6E8EA","cursor":"#3FC1B0","cursorAccent":"#0E1012","selection":"#1C2024","black":"#15181B","brightBlack":"#6B747D","red":"#F2707F","brightRed":"#F58A96","green":"#4CC985","brightGreen":"#6ED79C","yellow":"#E8B23A","brightYellow":"#EEC463","blue":"#7FB2F0","brightBlue":"#9AC4F4","magenta":"#A792F2","brightMagenta":"#BCAAF5","cyan":"#3FC1B0","brightCyan":"#63D0C1","white":"#C3C8CD","brightWhite":"#E6E8EA"}' ;;
    light) echo 'Set Theme {"name":"agent-core-light","background":"#FAFBFC","foreground":"#1F2328","cursor":"#0F766E","cursorAccent":"#FAFBFC","selection":"#DDE3E8","black":"#1F2328","brightBlack":"#59636E","red":"#B42334","brightRed":"#CF222E","green":"#16794A","brightGreen":"#1A7F37","yellow":"#8A5A00","brightYellow":"#9A6700","blue":"#0B5CAD","brightBlue":"#0969DA","magenta":"#6F42C1","brightMagenta":"#8250DF","cyan":"#0F766E","brightCyan":"#1B7C83","white":"#59636E","brightWhite":"#1F2328"}' ;;
  esac
  sed "s|@REPO@|$repo|g" "$here/$clip.tape.body"
} > "$work/$clip.tape"

# Run the command once off screen first: abort if its output would put a path or user name in the frame.
dry=$(cd "$repo" && env -u VIRTUAL_ENV UV_NO_PROGRESS=1 PYTHONWARNINGS=ignore uv run --extra otel python examples/approval_flow.py 2>&1)
if grep -q -F -e "$repo" -e "$HOME" -e "$USER" <<<"$dry"; then echo "output contains a path or user name; not recording" >&2; exit 1; fi

(cd "$repo" && vhs "$work/$clip.tape")

files=("$out/$out_name.gif")
if [ "$out_name" = "$clip" ]; then
  ffmpeg -v error -y -i "$work/$out_name.webm" -vf "fps=30,format=yuv420p" \
    -c:v libx264 -crf 27 -preset slow -movflags +faststart -an -bsf:v filter_units=remove_types=6 "$out/$out_name.mp4"
  files+=("$out/$out_name.mp4")
fi
# GIF: 900 px wide, palette method; halve the frame rate once if it lands over 3 MB.
for fps in 15 10; do
  ffmpeg -v error -y -i "$work/$out_name.webm" \
    -vf "fps=$fps,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none" \
    "$out/$out_name.gif"
  [ "$(stat -c%s "$out/$out_name.gif")" -le 3145728 ] && break
done

python3 "$here/strip_media_metadata.py" "${files[@]}"
python3 "$here/check_media_metadata.py" "${files[@]}"
ls -l "${files[@]}"

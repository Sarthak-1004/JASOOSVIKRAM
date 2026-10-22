#!/usr/bin/env bash
# ffmpeg + Hindi font + Python packages for the GitHub runner.
# The Ubuntu package mirrors sometimes hang for a long time, so every apt call has a time
# limit and is retried; if apt still fails we download ffmpeg and the font directly.
set -u
APT_OPTS=(-qq -o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)

for i in 1 2 3; do
  if sudo timeout 150 apt-get update "${APT_OPTS[@]}" &&
     sudo timeout 300 apt-get install -y "${APT_OPTS[@]}" ffmpeg fonts-noto-core >/dev/null; then
    break
  fi
  echo "apt attempt $i failed or timed out"
  sleep 5
done

if ! command -v ffmpeg >/dev/null; then
  echo "::warning title=install::Ubuntu mirrors failed; using a downloaded ffmpeg build"
  curl -fsSL --retry 4 --max-time 300 -o /tmp/ffmpeg.tar.xz \
    https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
  tar -xJf /tmp/ffmpeg.tar.xz -C /tmp
  sudo cp /tmp/ffmpeg-master-latest-linux64-gpl/bin/ff* /usr/local/bin/
fi

if ! fc-list 2>/dev/null | grep -q "Noto Sans Devanagari"; then
  echo "::warning title=install::Hindi font package missing; downloading Noto Sans Devanagari"
  mkdir -p assets/fonts
  base=https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSansDevanagari/hinted/ttf
  for w in Regular Bold; do
    curl -fsSL --retry 4 --max-time 120 -o "assets/fonts/NotoSansDevanagari-$w.ttf" \
      "$base/NotoSansDevanagari-$w.ttf"
  done
fi

ffmpeg -version | head -1
pip install -q -r requirements.txt

#!/usr/bin/env bash
# Render the feature clips and encode small looping web versions into docs/assets/clips.
# The poster (jpg) is taken mid-action, so a blocked autoplay still shows the feature at work.
set -euo pipefail
cd "$(dirname "$0")/.."
out=../../docs/assets/clips
mkdir -p out/clips "$out"
for id in ClipFiles ClipHttp ClipAgent ClipCache ClipBudget ClipNotify ClipUpsert ClipObserve; do
  name=$(echo "${id#Clip}" | tr '[:upper:]' '[:lower:]')
  npx remotion render src/index.ts "$id" "out/clips/$name.mp4" --codec=h264 --crf=18 --log=error
  ffmpeg -v error -y -i "out/clips/$name.mp4" -c:v libx264 -crf 28 -preset slow -pix_fmt yuv420p \
    -movflags +faststart -an "$out/$name.mp4"
  ffmpeg -v error -y -i "out/clips/$name.mp4" -c:v libvpx-vp9 -b:v 0 -crf 40 -row-mt 1 -an "$out/$name.webm"
  ffmpeg -v error -y -ss 5.8 -i "out/clips/$name.mp4" -frames:v 1 -q:v 4 "$out/$name.jpg"
done
ls -la "$out"

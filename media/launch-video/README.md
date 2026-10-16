# agentl launch video

Motion graphics for the agentl launch, written in React with [Remotion](https://www.remotion.dev/).
Everything is code, so the video can be re-rendered for every release.

## Render

```bash
cd media/launch-video
npm install
npm run studio        # live preview in the browser
npm run render        # out/agentl-launch.mp4 (1920x1080, 60 fps, camera motion blur)
npm run poster        # out/poster.jpg
```

Fonts (Inter, JetBrains Mono) are bundled from npm, so renders work offline.

## "From 253 lines to 45" video

`Compress` shows `examples/invoices/legacy_etl.py` collapsing into `examples/invoices/pipeline.yaml`.
The code on screen is generated from those files, so the video always matches the repo:

```bash
npm run sources           # regenerate src/compress/sources.ts from examples/invoices
npm run render:compress   # out/agentl-compress.mp4 (1920x1080, 60 fps, 29 s)
```

The terminal output in the "run" scene is illustrative and labelled as sample output.

## 30-second promo

`Promo` (1920x1080) and `PromoVertical` (1080x1920, for Reels and Shorts) share one component in
`src/Promo.tsx`, in the Carioca palette of `media/brand`. Six scenes: scattered data, the YAML,
a run, the re-run that the per-record cache answers for $0, the result, the install command.

```bash
npm run render:promo            # out/agentl-promo.mp4 (30 fps, 30 s)
npm run render:promo-vertical   # out/agentl-promo-vertical.mp4
```

The run numbers are sample output and labelled as such on screen.

`Terminal` and `TerminalVertical` (`src/Terminal.tsx`, 16 s): one fixed terminal where `lint`, `plan`,
`run` and a cached re-run stream in line by line, with entry and exit effects
(`npm run render:terminal`, `npm run render:terminal-vertical`). Sample output, labelled.

## Web version

The site uses a smaller encode of the master render:

```bash
ffmpeg -i out/agentl-launch.mp4 -vf scale=1280:-2 -c:v libx264 -crf 26 -preset slow \
  -movflags +faststart -an ../../docs/assets/agentl-launch.mp4
ffmpeg -i out/agentl-launch.mp4 -vf scale=1280:-2 -c:v libvpx-vp9 -b:v 0 -crf 38 -row-mt 1 \
  -an ../../docs/assets/agentl-launch.webm
```

## Editing

- Timeline and scenes: `src/Launch.tsx` (the `S` object holds each scene's start and length in frames).
- Colors and fonts: `src/theme.ts`.
- Numbers shown in the run scene are illustrative.

Remotion is free for individuals and small teams; check its license before using it in a larger company.

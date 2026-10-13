# agentl launch video

Motion graphics for the agentl launch, written in React with [Remotion](https://www.remotion.dev/).
Everything is code, so the video can be re-rendered for every release.

## Render

```bash
cd media/launch-video
npm install
npm run studio        # live preview in the browser
npm run render        # out/agentl-launch.mp4 (1920x1080, 60 fps)
npm run poster        # out/poster.jpg
```

Fonts (Inter, JetBrains Mono) are bundled from npm, so renders work offline.

## Web version

The site uses a smaller encode of the master render:

```bash
ffmpeg -i out/agentl-launch.mp4 -vf scale=1280:-2 -c:v libx264 -crf 26 -preset slow \
  -movflags +faststart -an ../../docs/assets/agentl-launch.mp4
```

## Editing

- Timeline and scenes: `src/Launch.tsx` (the `S` object holds each scene's start and length in frames).
- Colors and fonts: `src/theme.ts`.
- Numbers shown in the run scene are illustrative.

Remotion is free for individuals and small teams; check its license before using it in a larger company.

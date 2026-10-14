import { CameraMotionBlur } from "@remotion/motion-blur";
import { Composition } from "remotion";
import { CLIP_FPS, CLIP_FRAMES, CLIP_H, CLIP_W, CLIPS } from "./Clips";
import { Compress, COMPRESS_FRAMES } from "./Compress";
import { Launch, LAUNCH_FRAMES } from "./Launch";
import { FPS, H, W } from "./theme";
import { TEASER_FPS, TEASER_FRAMES, TEASER_SIZE, XTeaser } from "./XTeaser";

const LaunchWithBlur: React.FC = () => (
  <CameraMotionBlur samples={4} shutterAngle={180}>
    <Launch />
  </CameraMotionBlur>
);

const CompressWithBlur: React.FC = () => (
  <CameraMotionBlur samples={4} shutterAngle={180}>
    <Compress />
  </CameraMotionBlur>
);

export const Root: React.FC = () => (
  <>
    <Composition id="Launch" component={LaunchWithBlur} durationInFrames={LAUNCH_FRAMES} fps={FPS} width={W} height={H} />
    <Composition id="Compress" component={CompressWithBlur} durationInFrames={COMPRESS_FRAMES} fps={FPS} width={W} height={H} />
    {CLIPS.map(([id, Comp]) => (
      <Composition key={id} id={id} component={Comp} durationInFrames={CLIP_FRAMES} fps={CLIP_FPS} width={CLIP_W} height={CLIP_H} />
    ))}
    <Composition id="XTeaser" component={XTeaser} durationInFrames={TEASER_FRAMES} fps={TEASER_FPS} width={TEASER_SIZE} height={TEASER_SIZE} />
    <Composition id="LaunchPreview" component={Launch} durationInFrames={LAUNCH_FRAMES} fps={FPS} width={W} height={H} />
  </>
);

import { CameraMotionBlur } from "@remotion/motion-blur";
import { Composition } from "remotion";
import { CLIP_FPS, CLIP_FRAMES, CLIP_H, CLIP_W, CLIPS } from "./Clips";
import { Compress, COMPRESS_FRAMES } from "./Compress";
import { Launch, LAUNCH_FRAMES } from "./Launch";
import { Promo, PROMO_FPS, PROMO_FRAMES } from "./Promo";
import { Terminal, TERMINAL_FPS, TERMINAL_FRAMES } from "./Terminal";
import { FPS, H, W } from "./theme";
import { TEASER_FPS, TEASER_FRAMES, TEASER_SIZE, XTeaser } from "./XTeaser";

const LaunchWithBlur: React.FC = () => (
  <CameraMotionBlur samples={4} shutterAngle={180}>
    <Launch />
  </CameraMotionBlur>
);

const XTeaserWithBlur: React.FC = () => (
  <CameraMotionBlur samples={4} shutterAngle={180}>
    <XTeaser />
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
    <Composition id="XTeaser" component={XTeaserWithBlur} durationInFrames={TEASER_FRAMES} fps={TEASER_FPS} width={TEASER_SIZE} height={TEASER_SIZE} />
    <Composition id="Promo" component={Promo} durationInFrames={PROMO_FRAMES} fps={PROMO_FPS} width={1920} height={1080} />
    <Composition id="PromoVertical" component={Promo} durationInFrames={PROMO_FRAMES} fps={PROMO_FPS} width={1080} height={1920} />
    <Composition id="Terminal" component={Terminal} durationInFrames={TERMINAL_FRAMES} fps={TERMINAL_FPS} width={1920} height={1080} />
    <Composition id="TerminalVertical" component={Terminal} durationInFrames={TERMINAL_FRAMES} fps={TERMINAL_FPS} width={1080} height={1920} />
    <Composition id="LaunchPreview" component={Launch} durationInFrames={LAUNCH_FRAMES} fps={FPS} width={W} height={H} />
  </>
);

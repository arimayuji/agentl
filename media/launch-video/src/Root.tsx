import { CameraMotionBlur } from "@remotion/motion-blur";
import { Composition } from "remotion";
import { Launch, LAUNCH_FRAMES } from "./Launch";
import { FPS, H, W } from "./theme";

const LaunchWithBlur: React.FC = () => (
  <CameraMotionBlur samples={4} shutterAngle={180}>
    <Launch />
  </CameraMotionBlur>
);

export const Root: React.FC = () => (
  <>
    <Composition id="Launch" component={LaunchWithBlur} durationInFrames={LAUNCH_FRAMES} fps={FPS} width={W} height={H} />
    <Composition id="LaunchPreview" component={Launch} durationInFrames={LAUNCH_FRAMES} fps={FPS} width={W} height={H} />
  </>
);

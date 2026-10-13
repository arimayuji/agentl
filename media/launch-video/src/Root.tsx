import { Composition } from "remotion";
import { Launch, LAUNCH_FRAMES } from "./Launch";
import { FPS, H, W } from "./theme";

export const Root: React.FC = () => (
  <Composition id="Launch" component={Launch} durationInFrames={LAUNCH_FRAMES} fps={FPS} width={W} height={H} />
);

import React from "react";
import { Composition } from "remotion";
import { WizardVideo, FPS, TOTAL_FRAMES } from "./WizardVideo";

/**
 * Solo landscape: el video se embebe en un paso del wizard, dentro de una
 * pagina responsiva. No es una pieza para Reels.
 */
export const Root: React.FC = () => (
  <Composition
    id="WizardVideo"
    component={WizardVideo}
    durationInFrames={TOTAL_FRAMES}
    fps={FPS}
    width={1920}
    height={1080}
  />
);

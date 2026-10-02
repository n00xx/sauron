import React from "react";
import { Composition } from "remotion";
import { WizardVideo, FPS, TOTAL_FRAMES } from "./WizardVideo";
import { AntesVideo, ANTES_TOTAL_FRAMES } from "./AntesVideo";

/**
 * Solo landscape: los videos se embeben en pasos del wizard, dentro de una
 * pagina responsiva. No son piezas para Reels.
 */
export const Root: React.FC = () => (
  <>
    <Composition
      id="WizardVideo"
      component={WizardVideo}
      durationInFrames={TOTAL_FRAMES}
      fps={FPS}
      width={1920}
      height={1080}
    />
    <Composition
      id="AntesDeEmpezar"
      component={AntesVideo}
      durationInFrames={ANTES_TOTAL_FRAMES}
      fps={FPS}
      width={1920}
      height={1080}
    />
  </>
);

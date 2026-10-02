import React from "react";
import { AbsoluteFill, Sequence, staticFile } from "remotion";
import { Audio } from "@remotion/media";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { slide } from "@remotion/transitions/slide";

import { FPS, T } from "./WizardVideo";
import { SceneIntro } from "./scenes/antes/SceneIntro";
import { SceneRenovar } from "./scenes/antes/SceneRenovar";
import { SceneRecuperar } from "./scenes/antes/SceneRecuperar";
import { SceneOtroDispositivo } from "./scenes/antes/SceneOtroDispositivo";
import { SceneListo } from "./scenes/antes/SceneListo";
import { COLORS } from "./styles";

/**
 * Video del primer paso del wizard, "Algunas cosas que debes saber antes de
 * empezar". Reemplaza el texto y las seis capturas de ese paso.
 *
 * Duraciones medidas con ffprobe sobre public/voiceover/antes/*.mp3, mas ~1s de
 * aire. Si se regenera la voz hay que volver a medir: npm run durations
 *
 *   01-intro         9.20s      04-dispositivos 13.68s
 *   02-renovar      12.88s      05-cierre        3.68s
 *   03-recuperar     9.36s
 */
export const ANTES_SCENES = [
  10 * FPS, // intro
  14.5 * FPS, // renovar
  11 * FPS, // recuperar
  15.5 * FPS, // otro dispositivo
  5.5 * FPS, // listo
];

export const ANTES_TOTAL_FRAMES =
  ANTES_SCENES.reduce((a, b) => a + b, 0) - (ANTES_SCENES.length - 1) * T;

const sceneStarts = ANTES_SCENES.reduce<number[]>((acc, _dur, i) => {
  if (i === 0) return [0];
  acc.push(acc[i - 1] + ANTES_SCENES[i - 1] - T);
  return acc;
}, []);

const VOICEOVERS = [
  { file: "voiceover/antes/01-intro.mp3", delay: 0.5 },
  { file: "voiceover/antes/02-renovar.mp3", delay: 0.4 },
  { file: "voiceover/antes/03-recuperar.mp3", delay: 0.4 },
  { file: "voiceover/antes/04-dispositivos.mp3", delay: 0.4 },
  { file: "voiceover/antes/05-cierre.mp3", delay: 0.4 },
];

const cut = (durationInFrames: number) => linearTiming({ durationInFrames });

export const AntesVideo: React.FC = () => (
  <AbsoluteFill style={{ background: COLORS.bg }}>
    <TransitionSeries>
      <TransitionSeries.Sequence durationInFrames={ANTES_SCENES[0]}>
        <SceneIntro />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition presentation={fade()} timing={cut(T)} />

      {/* Los tres temas se deslizan: misma estructura, otro tema */}
      <TransitionSeries.Sequence durationInFrames={ANTES_SCENES[1]}>
        <SceneRenovar />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition
        presentation={slide({ direction: "from-right" })}
        timing={cut(T)}
      />

      <TransitionSeries.Sequence durationInFrames={ANTES_SCENES[2]}>
        <SceneRecuperar />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition
        presentation={slide({ direction: "from-right" })}
        timing={cut(T)}
      />

      <TransitionSeries.Sequence durationInFrames={ANTES_SCENES[3]}>
        <SceneOtroDispositivo />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition presentation={fade()} timing={cut(T)} />

      <TransitionSeries.Sequence durationInFrames={ANTES_SCENES[4]}>
        <SceneListo durationInFrames={ANTES_SCENES[4]} />
      </TransitionSeries.Sequence>
    </TransitionSeries>

    {/* Capa global de audio, por la misma razon que en WizardVideo */}
    {VOICEOVERS.map((vo, i) => (
      <Sequence key={vo.file} from={sceneStarts[i] + Math.round(vo.delay * FPS)}>
        <Audio src={staticFile(vo.file)} volume={1} />
      </Sequence>
    ))}
  </AbsoluteFill>
);

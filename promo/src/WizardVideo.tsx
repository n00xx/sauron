import React from "react";
import { AbsoluteFill, Sequence, staticFile } from "remotion";
import { Audio } from "@remotion/media";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { slide } from "@remotion/transitions/slide";

import { SceneGracias } from "./scenes/SceneGracias";
import { SceneDispositivos } from "./scenes/SceneDispositivos";
import { SceneInstalar } from "./scenes/SceneInstalar";
import { SceneDescargas } from "./scenes/SceneDescargas";
import { SceneConectar } from "./scenes/SceneConectar";
import { SceneCierre } from "./scenes/SceneCierre";
import { COLORS } from "./styles";

export const FPS = 30;
/** Duracion de cada transicion, en frames. */
export const T = 15;

/**
 * Duraciones medidas con ffprobe sobre public/voiceover/*.mp3, mas ~1.2s de aire.
 * Si se regenera la voz hay que volver a medir: npm run durations
 *
 *   01-gracias       7.92s      05-roku          6.16s
 *   02-dispositivos  8.00s      06-descargas    11.12s
 *   03-firetv        6.16s      07-conectar     20.88s
 *   04-googletv      7.28s      08-cierre       14.16s
 */
export const SCENES = [
  9.5 * FPS, // gracias
  9.5 * FPS, // dispositivos
  8 * FPS, // fire tv
  9 * FPS, // google tv
  8 * FPS, // roku
  13 * FPS, // descargas
  22.5 * FPS, // conectar
  16 * FPS, // cierre
];

export const TOTAL_FRAMES =
  SCENES.reduce((a, b) => a + b, 0) - (SCENES.length - 1) * T;

/**
 * TransitionSeries encima las escenas adyacentes por T frames, asi que el inicio
 * real de cada escena no es la suma simple de las anteriores.
 */
const sceneStarts = SCENES.reduce<number[]>((acc, _dur, i) => {
  if (i === 0) return [0];
  acc.push(acc[i - 1] + SCENES[i - 1] - T);
  return acc;
}, []);

const VOICEOVERS = [
  { file: "voiceover/01-gracias.mp3", delay: 0.5 },
  { file: "voiceover/02-dispositivos.mp3", delay: 0.4 },
  { file: "voiceover/03-firetv.mp3", delay: 0.4 },
  { file: "voiceover/04-googletv.mp3", delay: 0.4 },
  { file: "voiceover/05-roku.mp3", delay: 0.4 },
  { file: "voiceover/06-descargas.mp3", delay: 0.4 },
  { file: "voiceover/07-conectar.mp3", delay: 0.4 },
  { file: "voiceover/08-cierre.mp3", delay: 0.4 },
];

const cut = (durationInFrames: number) => linearTiming({ durationInFrames });

export const WizardVideo: React.FC = () => (
  <AbsoluteFill style={{ background: COLORS.bg }}>
    <TransitionSeries>
      <TransitionSeries.Sequence durationInFrames={SCENES[0]}>
        <SceneGracias />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition presentation={fade()} timing={cut(T)} />

      <TransitionSeries.Sequence durationInFrames={SCENES[1]}>
        <SceneDispositivos />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition presentation={fade()} timing={cut(T)} />

      <TransitionSeries.Sequence durationInFrames={SCENES[2]}>
        <SceneInstalar
          eyebrow="Fire TV"
          title="Instala la app desde la tienda"
          appName="Wholphin"
          image="img/firetv-steps.png"
          cta="Selecciona e instala"
          glowX="38%"
          steps={[
            "Abre la tienda de aplicaciones",
            'Selecciona búsqueda y escribe "Wholphin"',
            "Elige la app en los resultados",
          ]}
        />
      </TransitionSeries.Sequence>

      {/* Deslizar entre las tres tiendas: misma leccion, otra variante */}
      <TransitionSeries.Transition
        presentation={slide({ direction: "from-right" })}
        timing={cut(T)}
      />

      <TransitionSeries.Sequence durationInFrames={SCENES[3]}>
        <SceneInstalar
          eyebrow="Google TV"
          title="Mismo proceso, pero otra aplicación"
          appName="Moonfin"
          image="img/googletv-steps.png"
          cta="Selecciona e instala"
          glowX="50%"
          steps={[
            "Abre la tienda de aplicaciones",
            'Selecciona búsqueda y escribe "Moonfin"',
            "Elige la app en los resultados",
          ]}
        />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition
        presentation={slide({ direction: "from-right" })}
        timing={cut(T)}
      />

      <TransitionSeries.Sequence durationInFrames={SCENES[4]}>
        <SceneInstalar
          eyebrow="Roku"
          title="Aquí la aplicación es Jellyfin"
          appName="Jellyfin"
          image="img/roku-steps.png"
          cta="Añadir canal"
          glowX="62%"
          steps={[
            "Abre Canales de streaming",
            'Selecciona buscar y escribe "Jellyfin"',
            "Elige la app en los resultados",
          ]}
        />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition presentation={fade()} timing={cut(T)} />

      <TransitionSeries.Sequence durationInFrames={SCENES[5]}>
        <SceneDescargas />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition presentation={fade()} timing={cut(T)} />

      <TransitionSeries.Sequence durationInFrames={SCENES[6]}>
        <SceneConectar />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition presentation={fade()} timing={cut(T)} />

      <TransitionSeries.Sequence durationInFrames={SCENES[7]}>
        <SceneCierre />
      </TransitionSeries.Sequence>
    </TransitionSeries>

    {/*
      El audio va como capa global y no dentro de TransitionSeries: ahi dentro
      cada Sequence cortaria la locucion en el limite de la escena.
    */}
    {VOICEOVERS.map((vo, i) => (
      <Sequence key={vo.file} from={sceneStarts[i] + Math.round(vo.delay * FPS)}>
        <Audio src={staticFile(vo.file)} volume={1} />
      </Sequence>
    ))}
  </AbsoluteFill>
);

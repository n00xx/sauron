import React from "react";
import { Shot } from "../../components/Shots";
import { IconArrowDown } from "../../components/Icons";
import { StepsScene, VISUAL_H, VISUAL_W } from "./StepsScene";

/**
 * Tiempos medidos sobre la locucion real con:
 *   ffmpeg -i public/voiceover/antes/02-renovar.mp3 -af silencedetect=n=-32dB:d=0.25 -f null -
 *
 * El audio arranca en el frame 12 (delay de 0.4s); cada paso entra ~0.4s antes
 * de que la voz lo diga:
 *   "entra al enlace de renovacion"  -> audio 1.14s -> frame  46
 *   "Escribe tu usuario"             -> audio 5.00s -> frame 162
 *   "y toca Validar cuenta"          -> audio 7.30s -> frame 231
 *   "Luego elige la duracion"        -> audio 9.34s -> frame 292
 */
const ENLACE = 34;
const USUARIO = 150;
const VALIDAR = 219;
const PLAN = 280;

const fit = { maxW: VISUAL_W, maxH: VISUAL_H };

export const SceneRenovar: React.FC = () => (
  <StepsScene
    eyebrow="Renovar"
    title="Si te gustó Neexy, renovar es muy fácil"
    glowX="36%"
    pill={{
      lead: "El enlace está",
      value: (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          debajo del video <IconArrowDown size={30} color="#fff" />
        </span>
      ),
      at: ENLACE,
    }}
    steps={[
      { text: "Abre el enlace de renovación", at: ENLACE },
      { text: "Escribe tu usuario y tu contraseña", at: USUARIO },
      { text: <>Toca <strong>Validar cuenta</strong></>, at: VALIDAR },
      { text: "Elige la duración y el plan, y paga", at: PLAN },
    ]}
    visuals={[
      {
        from: ENLACE,
        node: <Shot src="img/antes/renovar-validar-cuenta.webp" w={1142} h={507} {...fit} />,
      },
      {
        from: PLAN,
        node: <Shot src="img/antes/renovar-cuenta-validada.webp" w={1126} h={801} {...fit} />,
      },
    ]}
  />
);

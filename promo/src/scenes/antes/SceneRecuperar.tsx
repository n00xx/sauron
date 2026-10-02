import React from "react";
import { Shot } from "../../components/Shots";
import { IconKey } from "../../components/Icons";
import { COLORS } from "../../styles";
import { monoFont } from "../../fonts";
import { StepsScene, VISUAL_H, VISUAL_W } from "./StepsScene";

/**
 * Tiempos medidos sobre la locucion real con:
 *   ffmpeg -i public/voiceover/antes/03-recuperar.mp3 -af silencedetect=n=-32dB:d=0.25 -f null -
 *
 * El audio arranca en el frame 12 (delay de 0.4s):
 *   "En neexy punto net"            -> audio 2.93s -> frame 100
 *   "toca Recuperar"                -> audio 4.47s -> frame 146
 *   "elige que necesitas"           -> audio 6.49s -> frame 207
 *   "y te lo enviamos a tu correo"  -> audio 8.04s -> frame 253
 */
const NEEXY = 88;
const RECUPERAR = 134;
const ELIGE = 195;
const CORREO = 241;

const fit = { maxW: VISUAL_W, maxH: VISUAL_H };

/** Mientras la voz hace la pregunta todavia no hay captura que enseñar. */
const KeyBadge: React.FC = () => (
  <div
    style={{
      width: 230,
      height: 230,
      borderRadius: 999,
      background: COLORS.primarySoft,
      border: `1px solid rgba(254,65,85,0.3)`,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
    }}
  >
    <IconKey size={112} color={COLORS.primary} />
  </div>
);

export const SceneRecuperar: React.FC = () => (
  <StepsScene
    eyebrow="Recuperar tu acceso"
    title="¿Olvidaste tu usuario o tu contraseña?"
    glowX="62%"
    pill={{
      lead: "Entra a",
      value: <span style={{ fontFamily: monoFont, fontWeight: 700 }}>neexy.net</span>,
      at: NEEXY,
    }}
    steps={[
      { text: <>Toca <strong>Recuperar</strong>, en el menú de arriba</>, at: RECUPERAR },
      { text: "Elige qué necesitas: tu usuario o tu contraseña", at: ELIGE },
      { text: "Te lo enviamos al correo con el que te registraste", at: CORREO },
    ]}
    visuals={[
      { from: 10, node: <KeyBadge /> },
      {
        from: NEEXY,
        node: <Shot src="img/antes/recuperar-menu.webp" w={597} h={116} {...fit} />,
      },
      {
        from: ELIGE,
        node: <Shot src="img/antes/recuperar-opciones.webp" w={1042} h={651} {...fit} />,
      },
    ]}
  />
);

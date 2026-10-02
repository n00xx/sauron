import React from "react";
import { useCurrentFrame } from "remotion";
import { Stage } from "../../components/Stage";
import { IconCheck } from "../../components/Icons";
import { COLORS, TEXT } from "../../styles";
import { fadeOut, fadeUp, popIn } from "../../anim";

/**
 * Puente hacia el siguiente paso del wizard, que es el video de instalacion.
 *   "Ahora si, vamos a instalar la app" -> audio 1.43s -> frame 55
 */
const FRASE = 46;

export const SceneListo: React.FC<{ durationInFrames: number }> = ({ durationInFrames }) => {
  const frame = useCurrentFrame();

  return (
    <Stage glowY="40%">
      {/* Ultima escena: no hay transicion despues, asi que se desvanece sola */}
      <div
        style={{
          ...fadeOut(frame, durationInFrames - 20, 18),
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
        }}
      >
        <div
          style={{
            ...fadeUp(frame, 6, 20),
            transform: `scale(${popIn(frame, 6, 18)})`,
            width: 150,
            height: 150,
            borderRadius: 999,
            background: COLORS.primarySoft,
            border: `1.5px solid rgba(254,65,85,0.4)`,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <IconCheck size={78} color={COLORS.primary} />
        </div>
        <h2
          style={{
            ...fadeUp(frame, FRASE, 26),
            margin: "44px 0 0",
            fontSize: TEXT.title,
            fontWeight: 800,
            letterSpacing: -1.4,
            textAlign: "center",
          }}
        >
          Ahora sí, vamos a instalar la app
        </h2>
        <p
          style={{
            ...fadeUp(frame, FRASE + 14, 22),
            margin: "22px 0 0",
            fontSize: TEXT.lead,
            color: COLORS.textMuted,
          }}
        >
          Te lo explicamos en el siguiente paso
        </p>
      </div>
    </Stage>
  );
};

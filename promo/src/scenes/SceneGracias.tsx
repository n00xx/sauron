import React from "react";
import { useCurrentFrame, useVideoConfig, spring } from "remotion";
import { Stage } from "../components/Stage";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../styles";
import { fadeUp, popIn } from "../anim";

/** Carteles del catalogo: rectangulos con gradiente, no imagenes reales. */
const POSTERS = [
  { hue: "#3b4a63", tilt: -7 },
  { hue: "#5a3d52", tilt: -3.5 },
  { hue: "#7d3742", tilt: 0 },
  { hue: "#4a4266", tilt: 3.5 },
  { hue: "#37485c", tilt: 7 },
];

export const SceneGracias: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const titleScale = spring({ frame, fps, config: { damping: 14, stiffness: 90 } });

  return (
    <Stage glowY="34%">
      <div
        style={{
          ...fadeUp(frame, 0, 30),
          transform: `scale(${titleScale}) translateY(${(1 - titleScale) * 20}px)`,
          textAlign: "center",
        }}
      >
        <h1
          style={{
            margin: 0,
            fontSize: TEXT.title,
            fontWeight: 800,
            letterSpacing: -2.2,
            lineHeight: 1.05,
          }}
        >
          ¡Gracias por unirte a{" "}
          <span style={{ color: COLORS.primary }}>Neexy</span>!
        </h1>
      </div>

      <p
        style={{
          ...fadeUp(frame, 26, 26),
          margin: "26px 0 0",
          fontSize: TEXT.lead,
          color: COLORS.textMuted,
          textAlign: "center",
          maxWidth: 1080,
          lineHeight: 1.45,
        }}
      >
        Ya tienes acceso a todo nuestro catálogo de películas y series.
      </p>

      {/* Fila de carteles: sugiere el catalogo sin mostrar titulos concretos */}
      <div
        style={{
          display: "flex",
          gap: 22,
          marginTop: 58,
          alignItems: "flex-end",
        }}
      >
        {POSTERS.map((p, i) => {
          const delay = 46 + i * 7;
          const scale = popIn(frame, delay, 22);
          return (
            <div
              key={i}
              style={{
                ...fadeUp(frame, delay, 26),
                width: 176,
                height: 258,
                borderRadius: RADIUS.chip,
                background: `linear-gradient(160deg, ${p.hue} 0%, ${COLORS.bgRaised} 100%)`,
                border: `1px solid ${COLORS.borderHi}`,
                boxShadow: SHADOW.card,
                transform: `translateY(${(1 - scale) * 26}px) rotate(${p.tilt}deg) scale(${scale})`,
              }}
            />
          );
        })}
      </div>

      <div
        style={{
          ...fadeUp(frame, 92, 22),
          marginTop: 44,
          fontSize: TEXT.label,
          color: COLORS.textDim,
          letterSpacing: 0.4,
        }}
      >
        Te explicamos cómo empezar en menos de dos minutos
      </div>
    </Stage>
  );
};

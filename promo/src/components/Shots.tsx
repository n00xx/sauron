import React from "react";
import { Img, interpolate, staticFile } from "remotion";
import { COLORS, RADIUS, SHADOW } from "../styles";

const CLAMP = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

/** Fundido entre un visual y el siguiente, en frames. */
const FADE = 12;

/**
 * Las capturas de origen miden entre 597 y 1142 px de ancho. Mas de 1.5x se
 * nota borroso en 1080p, asi que las chicas se quedan chicas.
 */
const MAX_UPSCALE = 1.5;

interface ShotProps {
  src: string;
  /** Tamaño real del archivo: con el se calcula la escala sin deformar. */
  w: number;
  h: number;
  maxW: number;
  maxH: number;
}

/** Captura enmarcada como las de las escenas de TV. */
export const Shot: React.FC<ShotProps> = ({ src, w, h, maxW, maxH }) => {
  const scale = Math.min(MAX_UPSCALE, maxW / w, maxH / h);
  return (
    <div
      style={{
        width: Math.round(w * scale),
        borderRadius: RADIUS.card,
        overflow: "hidden",
        border: `1px solid ${COLORS.borderHi}`,
        boxShadow: SHADOW.card,
        background: COLORS.bgRaised,
        lineHeight: 0,
      }}
    >
      <Img src={staticFile(src)} style={{ width: "100%", display: "block" }} />
    </div>
  );
};

export interface Visual {
  /** Frame en que entra; se queda hasta que entra el siguiente. */
  from: number;
  node: React.ReactNode;
}

interface CrossfadeProps {
  frame: number;
  visuals: Visual[];
  width: number;
  height: number;
}

/**
 * Apila los visuales en un hueco de tamaño fijo. Las capturas tienen
 * proporciones distintas; sin el hueco fijo, el layout saltaria en cada cambio.
 */
export const Crossfade: React.FC<CrossfadeProps> = ({ frame, visuals, width, height }) => (
  <div style={{ position: "relative", width, height, flexShrink: 0 }}>
    {visuals.map((v, i) => {
      const next = visuals[i + 1];
      const fadeIn = interpolate(frame, [v.from, v.from + FADE], [0, 1], CLAMP);
      const fadeOut = next
        ? interpolate(frame, [next.from, next.from + FADE], [1, 0], CLAMP)
        : 1;
      const scale = interpolate(frame, [v.from, v.from + FADE + 6], [0.96, 1], CLAMP);
      return (
        <div
          key={v.from}
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            opacity: Math.min(fadeIn, fadeOut),
            transform: `scale(${scale})`,
          }}
        >
          {v.node}
        </div>
      );
    })}
  </div>
);

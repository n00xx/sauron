import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { COLORS, GRADIENTS, RADIUS, TEXT } from "../styles";
import { uiFont } from "../fonts";
import { fadeUp } from "../anim";

interface StageProps {
  /** Etiqueta corta sobre el titulo, en mayusculas. */
  eyebrow?: string;
  title?: string;
  /** Desplaza el halo de acento para que cada escena no se sienta identica. */
  glowX?: string;
  glowY?: string;
  children?: React.ReactNode;
}

/**
 * Fondo comun a todas las escenas: gradiente base, halo de acento y grano.
 * Centraliza el encabezado para que el ritmo vertical sea el mismo en todas.
 */
export const Stage: React.FC<StageProps> = ({
  eyebrow,
  title,
  glowX = "50%",
  glowY = "22%",
  children,
}) => {
  const frame = useCurrentFrame();

  return (
    <AbsoluteFill
      style={{
        background: GRADIENTS.stage,
        fontFamily: uiFont,
        color: COLORS.text,
        overflow: "hidden",
      }}
    >
      {/* Halo de acento */}
      <div
        style={{
          position: "absolute",
          left: glowX,
          top: glowY,
          width: 1500,
          height: 1100,
          marginLeft: -750,
          marginTop: -550,
          background: GRADIENTS.glow,
          pointerEvents: "none",
        }}
      />

      <Grain />

      <div
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: "72px 108px",
        }}
      >
        {eyebrow ? (
          <div
            style={{
              ...fadeUp(frame, 0, 18),
              fontSize: TEXT.eyebrow,
              fontWeight: 700,
              letterSpacing: 3.4,
              textTransform: "uppercase",
              color: COLORS.primary,
              background: COLORS.primarySoft,
              border: `1px solid rgba(254, 65, 85, 0.28)`,
              borderRadius: RADIUS.pill,
              padding: "9px 26px",
              marginBottom: 26,
            }}
          >
            {eyebrow}
          </div>
        ) : null}

        {title ? (
          <h2
            style={{
              ...fadeUp(frame, 5, 26),
              margin: 0,
              fontSize: TEXT.titleSm,
              fontWeight: 800,
              letterSpacing: -1.2,
              textAlign: "center",
              lineHeight: 1.12,
              maxWidth: 1500,
            }}
          >
            {title}
          </h2>
        ) : null}

        {children}
      </div>
    </AbsoluteFill>
  );
};

/** Textura fija de grano: rompe el banding de los gradientes oscuros. */
const Grain: React.FC = () => (
  <svg
    style={{ position: "absolute", inset: 0, width: "100%", height: "100%", opacity: 0.05 }}
  >
    <filter id="grain">
      <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="3" />
    </filter>
    <rect width="100%" height="100%" filter="url(#grain)" />
  </svg>
);

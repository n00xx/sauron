import React from "react";
import { useCurrentFrame, useVideoConfig, spring } from "remotion";
import { Stage } from "../components/Stage";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../styles";
import { monoFont } from "../fonts";
import { fadeUp, pulse } from "../anim";
import { IconBook } from "../components/Icons";

export const SceneCierre: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const badge = spring({ frame: frame - 16, fps, config: { damping: 12, stiffness: 110 } });
  const glow = pulse(frame, 0.05, 0.3, 0.9);

  return (
    <Stage glowY="40%">
      <div
        style={{
          ...fadeUp(frame, 0, 30),
          width: 116,
          height: 116,
          borderRadius: RADIUS.pill,
          background: COLORS.primarySoft,
          border: `1px solid rgba(254,65,85,0.32)`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          transform: `scale(${Math.max(0, badge)})`,
          marginBottom: 34,
        }}
      >
        <IconBook size={56} color={COLORS.primary} />
      </div>

      <h2
        style={{
          ...fadeUp(frame, 14, 28),
          margin: 0,
          fontSize: TEXT.title,
          fontWeight: 800,
          letterSpacing: -2,
          textAlign: "center",
          lineHeight: 1.08,
        }}
      >
        ¿Te quedó alguna duda?
      </h2>

      <p
        style={{
          ...fadeUp(frame, 34, 26),
          margin: "24px 0 0",
          fontSize: TEXT.lead,
          color: COLORS.textMuted,
          textAlign: "center",
          maxWidth: 1120,
          lineHeight: 1.45,
        }}
      >
        En la sección <strong style={{ color: COLORS.text }}>Guías</strong> encontrarás
        más tutoriales de configuración.
      </p>

      <div
        style={{
          ...fadeUp(frame, 56, 34),
          marginTop: 50,
          padding: "30px 62px",
          borderRadius: RADIUS.card,
          background: COLORS.surface,
          border: `1.5px solid rgba(254,65,85,0.45)`,
          boxShadow: `${SHADOW.card}, 0 0 ${glow * 58}px rgba(254,65,85,${glow * 0.3})`,
          fontFamily: monoFont,
          fontSize: TEXT.mono,
          fontWeight: 700,
          letterSpacing: -0.5,
        }}
      >
        neexy.net/blog
      </div>

      <div
        style={{
          ...fadeUp(frame, 92, 24),
          marginTop: 52,
          fontSize: 40,
          fontWeight: 700,
          color: COLORS.text,
          textAlign: "center",
          lineHeight: 1.3,
        }}
      >
        Ya con esto estás listo.
      </div>

      <div
        style={{
          ...fadeUp(frame, 112, 24),
          marginTop: 14,
          fontSize: 36,
          fontWeight: 600,
          color: COLORS.textMuted,
          textAlign: "center",
        }}
      >
        Solo falta que prepares las palomitas 🍿
      </div>
    </Stage>
  );
};

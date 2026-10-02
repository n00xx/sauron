import React from "react";
import { useCurrentFrame, interpolateColors } from "remotion";
import { Stage } from "../../components/Stage";
import { IconKey, IconPhone, IconRefresh } from "../../components/Icons";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../../styles";
import { fadeUp, sweep } from "../../anim";

/**
 * Tiempos medidos sobre la locucion real con:
 *   ffmpeg -i public/voiceover/antes/01-intro.mp3 -af silencedetect=n=-32dB:d=0.25 -f null -
 *
 * El audio arranca en el frame 15 (delay de 0.5s); cada tarjeta se enciende
 * ~0.4s antes de que la voz la nombre:
 *   "como renovar"                  -> audio 4.16s -> frame 140
 *   "como recuperar tu acceso"      -> audio 5.24s -> frame 172
 *   "y como cambiar de dispositivo" -> audio 7.00s -> frame 225
 */
const ITEMS = [
  { Icon: IconRefresh, label: "Cómo renovar", lightAt: 128 },
  { Icon: IconKey, label: "Cómo recuperar\ntu acceso", lightAt: 160 },
  { Icon: IconPhone, label: "Cómo cambiar\nde dispositivo", lightAt: 213 },
];

const LIT_BORDER = "rgba(254, 65, 85, 0.5)";

/** Las tres tarjetas estan desde el principio, apagadas; la voz las va encendiendo. */
export const SceneIntro: React.FC = () => {
  const frame = useCurrentFrame();

  return (
    <Stage eyebrow="Antes de empezar" title="Tres cosas que te van a servir más adelante">
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: 36,
          marginTop: 64,
          width: "100%",
          maxWidth: 1300,
        }}
      >
        {ITEMS.map(({ Icon, label, lightAt }, i) => {
          const lit = sweep(frame, lightAt, 14);
          return (
            <div
              key={label}
              style={{
                ...fadeUp(frame, 22 + i * 9, 30),
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 22,
                padding: "44px 24px",
                borderRadius: RADIUS.card,
                background: GRADIENTS.card,
                border: `1.5px solid ${interpolateColors(lit, [0, 1], [COLORS.border, LIT_BORDER])}`,
                boxShadow: `${SHADOW.lift}, 0 0 ${lit * 48}px rgba(254,65,85,${lit * 0.22})`,
              }}
            >
              <div
                style={{
                  width: 104,
                  height: 104,
                  borderRadius: RADIUS.pill,
                  background: COLORS.primarySoft,
                  border: `1px solid rgba(254,65,85,0.26)`,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  opacity: 0.4 + lit * 0.6,
                }}
              >
                <Icon size={52} color={COLORS.primary} />
              </div>
              <div
                style={{
                  fontSize: TEXT.lead,
                  fontWeight: 700,
                  textAlign: "center",
                  lineHeight: 1.25,
                  whiteSpace: "pre-line",
                  opacity: 0.4 + lit * 0.6,
                }}
              >
                {label}
              </div>
            </div>
          );
        })}
      </div>
    </Stage>
  );
};

import React from "react";
import { useCurrentFrame } from "remotion";
import { Stage } from "../components/Stage";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../styles";
import { fadeUp, popIn } from "../anim";
import {
  IconPhone,
  IconLaptop,
  IconTv,
  IconGamepad,
} from "../components/Icons";

/** El texto en pantalla es etiqueta corta; el detalle lo lleva la locucion. */
const DEVICES = [
  { Icon: IconPhone, label: "Celulares\ny tablets" },
  { Icon: IconLaptop, label: "Windows, macOS\ny Linux" },
  { Icon: IconTv, label: "Smart TV\ny streaming" },
  { Icon: IconGamepad, label: "PlayStation\ny Xbox" },
];

export const SceneDispositivos: React.FC = () => {
  const frame = useCurrentFrame();

  return (
    <Stage eyebrow="Mira donde quieras" title="Neexy funciona en casi cualquier pantalla">
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, 1fr)",
          gap: 30,
          marginTop: 62,
          width: "100%",
          maxWidth: 1400,
        }}
      >
        {DEVICES.map(({ Icon, label }, i) => {
          const delay = 24 + i * 9;
          const scale = popIn(frame, delay, 20);
          return (
            <div
              key={label}
              style={{
                ...fadeUp(frame, delay, 30),
                transform: `translateY(${(1 - scale) * 30}px) scale(${scale})`,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 20,
                padding: "40px 20px",
                borderRadius: RADIUS.card,
                background: GRADIENTS.card,
                border: `1px solid ${COLORS.border}`,
                boxShadow: SHADOW.lift,
              }}
            >
              <div
                style={{
                  width: 92,
                  height: 92,
                  borderRadius: RADIUS.pill,
                  background: COLORS.primarySoft,
                  border: `1px solid rgba(254,65,85,0.26)`,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Icon size={46} color={COLORS.primary} />
              </div>
              <div
                style={{
                  fontSize: TEXT.label,
                  fontWeight: 600,
                  textAlign: "center",
                  lineHeight: 1.32,
                  color: COLORS.text,
                  whiteSpace: "pre-line",
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

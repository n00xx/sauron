import React from "react";
import { useCurrentFrame } from "remotion";
import { Stage } from "../components/Stage";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../styles";
import { monoFont } from "../fonts";
import { fadeUp, popIn, pulse } from "../anim";
import { IconPhone, IconTablet, IconLaptop, IconDownload } from "../components/Icons";

/**
 * Dos grupos: lo que se lleva en la mano y lo que se usa sentado. Separarlos
 * deja claro que la misma pagina de descarga sirve para ambos.
 */
const GROUPS = [
  {
    title: "Celulares y tablets",
    items: [
      { Icon: IconPhone, label: "Android" },
      { Icon: IconPhone, label: "iOS" },
      { Icon: IconTablet, label: "Tablets" },
    ],
  },
  {
    title: "Computadoras y laptops",
    items: [
      { Icon: IconLaptop, label: "Windows" },
      { Icon: IconLaptop, label: "Mac" },
    ],
  },
];

export const SceneDescargas: React.FC = () => {
  const frame = useCurrentFrame();
  const urlScale = popIn(frame, 58, 24);
  const glow = pulse(frame, 0.055, 0.3, 0.85);

  return (
    <Stage
      eyebrow="Celulares, tablets y computadoras"
      title="Descarga la app para tu dispositivo"
      glowX="66%"
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 52,
          marginTop: 50,
        }}
      >
        {GROUPS.map((group, gi) => (
          <div
            key={group.title}
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 20,
            }}
          >
            <div
              style={{
                ...fadeUp(frame, 16 + gi * 18, 22),
                fontSize: 21,
                fontWeight: 700,
                letterSpacing: 2,
                textTransform: "uppercase",
                color: COLORS.textDim,
              }}
            >
              {group.title}
            </div>
            <div style={{ display: "flex", gap: 20 }}>
              {group.items.map(({ Icon, label }, i) => (
                <div
                  key={label}
                  style={{
                    ...fadeUp(frame, 22 + gi * 18 + i * 8, 26),
                    display: "flex",
                    alignItems: "center",
                    gap: 14,
                    padding: "20px 32px",
                    borderRadius: RADIUS.pill,
                    background: GRADIENTS.card,
                    border: `1px solid ${COLORS.border}`,
                    fontSize: TEXT.label,
                    fontWeight: 600,
                  }}
                >
                  <Icon size={32} color={COLORS.primary} />
                  {label}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* La URL es lo unico que el usuario tiene que retener de esta escena */}
      <div
        style={{
          ...fadeUp(frame, 58, 34),
          transform: `scale(${urlScale})`,
          marginTop: 52,
          display: "flex",
          alignItems: "center",
          gap: 24,
          padding: "30px 56px",
          borderRadius: RADIUS.card,
          background: COLORS.surface,
          border: `1.5px solid rgba(254,65,85,0.45)`,
          boxShadow: `${SHADOW.card}, 0 0 ${glow * 56}px rgba(254,65,85,${glow * 0.3})`,
        }}
      >
        <IconDownload size={46} color={COLORS.primary} />
        <span
          style={{
            fontFamily: monoFont,
            fontSize: TEXT.mono,
            fontWeight: 700,
            letterSpacing: -0.5,
          }}
        >
          neexy.net/descargar
        </span>
      </div>

      <p
        style={{
          ...fadeUp(frame, 88, 22),
          marginTop: 30,
          fontSize: TEXT.body,
          color: COLORS.textMuted,
          textAlign: "center",
        }}
      >
        Entra y selecciona tu dispositivo correspondiente
      </p>
    </Stage>
  );
};

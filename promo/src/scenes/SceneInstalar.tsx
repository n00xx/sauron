import React from "react";
import { useCurrentFrame, Img, staticFile } from "remotion";
import { Stage } from "../components/Stage";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../styles";
import { fadeUp, popIn } from "../anim";

interface SceneInstalarProps {
  eyebrow: string;
  title: string;
  /** Nombre de la app en la tienda. Cambia entre Fire TV y Roku. */
  appName: string;
  image: string;
  steps: string[];
  /** Verbo final del flujo: "Instalar" en Fire TV, "Añadir canal" en Roku. */
  cta: string;
  glowX?: string;
}

/**
 * Marco compartido por Fire TV/Google TV y Roku. Solo cambian el nombre de la
 * app y la captura, asi que se lee como una seccion con variante y no como dos
 * lecciones distintas.
 *
 * Las capturas de origen son pequeñas (605x421 y 552x403), asi que aqui se
 * muestran con un upscale moderado y las etiquetas legibles se dibujan nativas
 * al lado, en texto vectorial nitido.
 */
export const SceneInstalar: React.FC<SceneInstalarProps> = ({
  eyebrow,
  title,
  appName,
  image,
  steps,
  cta,
  glowX = "50%",
}) => {
  const frame = useCurrentFrame();
  const imgScale = popIn(frame, 26, 26);

  return (
    <Stage eyebrow={eyebrow} title={title} glowX={glowX}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 62,
          marginTop: 46,
          width: "100%",
          maxWidth: 1660,
        }}
      >
        {/* Columna de pasos: texto nativo, siempre nitido */}
        <div
          style={{
            width: 600,
            flexShrink: 0,
            display: "flex",
            flexDirection: "column",
            gap: 22,
          }}
        >
          <div
            style={{
              ...fadeUp(frame, 18, 26),
              display: "inline-flex",
              alignSelf: "flex-start",
              alignItems: "center",
              gap: 14,
              padding: "14px 28px",
              borderRadius: RADIUS.pill,
              background: GRADIENTS.accentPill,
              boxShadow: SHADOW.lift,
            }}
          >
            <span style={{ fontSize: 22, fontWeight: 600, opacity: 0.85, color: "#fff" }}>
              Busca la app
            </span>
            <span style={{ fontSize: 32, fontWeight: 800, color: "#fff" }}>
              {appName}
            </span>
          </div>

          {steps.map((s, i) => (
            <div
              key={s}
              style={{
                ...fadeUp(frame, 34 + i * 12, 28),
                display: "flex",
                alignItems: "center",
                gap: 20,
                fontSize: TEXT.body,
                color: COLORS.text,
                lineHeight: 1.35,
              }}
            >
              <div
                style={{
                  flexShrink: 0,
                  width: 44,
                  height: 44,
                  borderRadius: RADIUS.pill,
                  background: COLORS.surfaceHi,
                  border: `1px solid ${COLORS.border}`,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 23,
                  fontWeight: 800,
                  color: COLORS.textMuted,
                }}
              >
                {i + 1}
              </div>
              {s}
            </div>
          ))}

          <div
            style={{
              ...fadeUp(frame, 34 + steps.length * 12, 26),
              marginTop: 10,
              alignSelf: "flex-start",
              padding: "14px 34px",
              borderRadius: RADIUS.pill,
              border: `1.5px solid ${COLORS.primary}`,
              color: COLORS.primary,
              fontSize: 27,
              fontWeight: 700,
            }}
          >
            {cta}
          </div>
        </div>

        {/* Captura de los pasos reales en pantalla */}
        <div
          style={{
            ...fadeUp(frame, 26, 34),
            flex: 1,
            transform: `scale(${imgScale})`,
            borderRadius: RADIUS.card,
            overflow: "hidden",
            border: `1px solid ${COLORS.borderHi}`,
            boxShadow: SHADOW.card,
            background: COLORS.bgRaised,
            lineHeight: 0,
          }}
        >
          <Img src={staticFile(image)} style={{ width: "100%", display: "block" }} />
        </div>
      </div>
    </Stage>
  );
};

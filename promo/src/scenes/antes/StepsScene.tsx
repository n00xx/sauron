import React from "react";
import { useCurrentFrame } from "remotion";
import { Stage } from "../../components/Stage";
import { StepList, TimedStep } from "../../components/StepList";
import { Crossfade, Visual } from "../../components/Shots";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../../styles";
import { fadeUp } from "../../anim";

/** Hueco de la columna derecha. Las capturas se escalan para caber aqui. */
export const VISUAL_W = 880;
export const VISUAL_H = 600;

interface Pill {
  lead: string;
  value: React.ReactNode;
  at: number;
}

interface StepsSceneProps {
  eyebrow: string;
  title: string;
  glowX?: string;
  /** Mismo sello rojo que "Busca la app Moonfin" del otro video. */
  pill?: Pill;
  /** Contexto en texto corrido, para cuando lo primero que dice la voz no es un paso. */
  note?: { text: React.ReactNode; at: number };
  steps: TimedStep[];
  visuals: Visual[];
}

/**
 * Marco comun de renovar, recuperar y cambiar de dispositivo: pasos a la
 * izquierda, captura real a la derecha. Es el mismo esquema que SceneInstalar,
 * pero con cada paso y cada captura atados a un frame medido sobre la voz.
 */
export const StepsScene: React.FC<StepsSceneProps> = ({
  eyebrow,
  title,
  glowX = "50%",
  pill,
  note,
  steps,
  visuals,
}) => {
  const frame = useCurrentFrame();

  return (
    <Stage eyebrow={eyebrow} title={title} glowX={glowX}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 60,
          marginTop: 40,
          width: "100%",
          maxWidth: 1600,
        }}
      >
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            gap: 24,
          }}
        >
          {pill ? (
            <div
              style={{
                ...fadeUp(frame, pill.at, 26),
                display: "inline-flex",
                alignSelf: "flex-start",
                alignItems: "center",
                gap: 14,
                padding: "14px 28px",
                marginBottom: 6,
                borderRadius: RADIUS.pill,
                background: GRADIENTS.accentPill,
                boxShadow: SHADOW.lift,
                color: "#fff",
              }}
            >
              <span style={{ fontSize: 22, fontWeight: 600, opacity: 0.85 }}>{pill.lead}</span>
              <span style={{ fontSize: 30, fontWeight: 800 }}>{pill.value}</span>
            </div>
          ) : null}

          {note ? (
            <p
              style={{
                ...fadeUp(frame, note.at, 24),
                margin: "0 0 8px",
                fontSize: TEXT.body,
                lineHeight: 1.4,
                color: COLORS.textMuted,
              }}
            >
              {note.text}
            </p>
          ) : null}

          <StepList frame={frame} steps={steps} />
        </div>

        <Crossfade frame={frame} visuals={visuals} width={VISUAL_W} height={VISUAL_H} />
      </div>
    </Stage>
  );
};

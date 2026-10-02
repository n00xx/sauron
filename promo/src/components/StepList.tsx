import React from "react";
import { COLORS, RADIUS, TEXT } from "../styles";
import { fadeUp } from "../anim";
import { IconCheck } from "./Icons";

export interface TimedStep {
  text: React.ReactNode;
  /** Frame en que la voz empieza a decirlo (ya con ~0.4 s de adelanto). */
  at: number;
}

type StepState = "pending" | "active" | "done";

const stateOf = (frame: number, step: TimedStep, next?: TimedStep): StepState => {
  if (frame < step.at) return "pending";
  if (next && frame >= next.at) return "done";
  return "active";
};

/**
 * Pasos numerados que aparecen al ritmo de la voz. El que se esta narrando va
 * en rojo; los ya dichos se marcan con palomita para que se vea el avance.
 */
export const StepList: React.FC<{ frame: number; steps: TimedStep[] }> = ({
  frame,
  steps,
}) => (
  <>
    {steps.map((step, i) => {
      const state = stateOf(frame, step, steps[i + 1]);
      return (
        <div
          key={i}
          style={{
            ...fadeUp(frame, step.at, 28),
            display: "flex",
            alignItems: "center",
            gap: 20,
            fontSize: TEXT.body,
            color: state === "done" ? COLORS.textMuted : COLORS.text,
            lineHeight: 1.35,
          }}
        >
          <div
            style={{
              flexShrink: 0,
              width: 44,
              height: 44,
              borderRadius: RADIUS.pill,
              background: state === "active" ? COLORS.primary : COLORS.surfaceHi,
              border: `1px solid ${state === "active" ? COLORS.primary : COLORS.border}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 23,
              fontWeight: 800,
              color: state === "active" ? "#fff" : COLORS.textMuted,
            }}
          >
            {state === "done" ? <IconCheck size={24} color={COLORS.success} /> : i + 1}
          </div>
          <span>{step.text}</span>
        </div>
      );
    })}
  </>
);

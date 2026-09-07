import { interpolate } from "remotion";

const CLAMP = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

/** Entrada estandar: aparece y sube. Devuelve estilos listos para el spread. */
export const fadeUp = (frame: number, delay = 0, distance = 34, span = 20) => ({
  opacity: interpolate(frame, [delay, delay + span], [0, 1], CLAMP),
  transform: `translateY(${interpolate(
    frame,
    [delay, delay + span],
    [distance, 0],
    CLAMP
  )}px)`,
});

/** Salida al final de la escena, para que nada se corte de golpe en la transicion. */
export const fadeOut = (frame: number, start: number, span = 14) => ({
  opacity: interpolate(frame, [start, start + span], [1, 0], CLAMP),
});

/** Escala de entrada con asentamiento; util para badges y sellos. */
export const popIn = (frame: number, delay = 0, span = 18) =>
  interpolate(frame, [delay, delay + span], [0.86, 1], CLAMP);

/** Barrido de 0 a 1 para barras de progreso y subrayados. */
export const sweep = (frame: number, delay: number, span = 24) =>
  interpolate(frame, [delay, delay + span], [0, 1], CLAMP);

/** Latido suave e infinito para elementos que deben llamar la atencion. */
export const pulse = (frame: number, speed = 0.07, min = 0.55, max = 1) =>
  interpolate(Math.sin(frame * speed), [-1, 1], [min, max]);

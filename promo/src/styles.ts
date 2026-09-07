/**
 * Paleta tomada de la UI real de Neexy.
 * El primario sale de app/static/css/main.css:263 (--color-primary: #fe4155).
 */
export const COLORS = {
  primary: "#fe4155",
  primaryDark: "#982633",
  primarySoft: "rgba(254, 65, 85, 0.14)",

  bg: "#0d121b",
  bgRaised: "#141b26",
  surface: "#1a2331",
  surfaceHi: "#222d3d",

  border: "rgba(255, 255, 255, 0.09)",
  borderHi: "rgba(255, 255, 255, 0.16)",

  text: "#f2f5f8",
  textMuted: "#93a1b3",
  textDim: "#63748a",

  success: "#3ecf8e",
} as const;

export const GRADIENTS = {
  /** Fondo base de todas las escenas. */
  stage: `radial-gradient(120% 90% at 50% -10%, #1d2736 0%, ${COLORS.bg} 62%)`,
  /** Halo de acento detrás del contenido principal. */
  glow: `radial-gradient(50% 50% at 50% 50%, rgba(254, 65, 85, 0.22) 0%, rgba(254, 65, 85, 0) 70%)`,
  /** Relleno de tarjetas: sutil, con luz desde arriba-izquierda. */
  card: `linear-gradient(150deg, rgba(255,255,255,0.07) 0%, rgba(255,255,255,0.02) 100%)`,
  accentPill: `linear-gradient(135deg, ${COLORS.primary} 0%, ${COLORS.primaryDark} 100%)`,
} as const;

/** Escala tipográfica para 1920x1080. */
export const TEXT = {
  eyebrow: 26,
  title: 76,
  titleSm: 58,
  lead: 36,
  body: 30,
  label: 26,
  mono: 44,
} as const;

export const RADIUS = {
  card: 22,
  pill: 999,
  chip: 16,
} as const;

/** Sombra reutilizable para superficies elevadas. */
export const SHADOW = {
  card: "0 24px 60px rgba(0, 0, 0, 0.45)",
  lift: "0 12px 34px rgba(0, 0, 0, 0.38)",
} as const;

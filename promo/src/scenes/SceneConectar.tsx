import React from "react";
import { useCurrentFrame, interpolate } from "remotion";
import { Stage } from "../components/Stage";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../styles";
import { monoFont } from "../fonts";
import { fadeUp, pulse } from "../anim";
import { IconTv, IconGamepad, IconLaptop, IconPhone } from "../components/Icons";

const TARGETS = [
  { Icon: IconTv, label: "Smart TV, Fire TV,\nRoku, proyector", active: true },
  { Icon: IconGamepad, label: "Apple TV, Xbox", active: false },
  { Icon: IconLaptop, label: "Samsung TV", active: false },
  { Icon: IconPhone, label: "Celular o\ncomputadora", active: false },
];

/**
 * Tiempos medidos sobre la locucion real con:
 *   ffmpeg -i public/voiceover/07-conectar.mp3 -af silencedetect=n=-32dB:d=0.30 -f null -
 *
 * El audio arranca en el frame 12 (delay de 0.4s), asi que cada paso se revela
 * ~0.4s antes de que la voz lo mencione:
 *   "Luego elige Quick Connect"  -> audio 11.19s -> frame 348
 *   "Escribelo en la pagina"     -> audio 15.70s -> frame 483
 */
const PASO2_DELAY = 336;
const PASO3_DELAY = 471;

/** El codigo se va escribiendo solo, para que se entienda que ahi va lo que sale en la tele. */
const CODE = "482913";
const CODE_START = 500;

export const SceneConectar: React.FC = () => {
  const frame = useCurrentFrame();
  const addrGlow = pulse(frame, 0.05, 0.28, 0.9);
  const typed = Math.floor(
    interpolate(frame, [CODE_START, CODE_START + 46], [0, CODE.length], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    })
  );

  return (
    <Stage eyebrow="Ya instalaste la app" title="Conecta tu primer dispositivo" glowY="18%">
      <div
        style={{
          display: "flex",
          gap: 56,
          marginTop: 40,
          width: "100%",
          maxWidth: 1680,
          alignItems: "flex-start",
        }}
      >
        {/* Selector de dispositivo */}
        <div style={{ width: 560, flexShrink: 0 }}>
          <div
            style={{
              ...fadeUp(frame, 8, 20),
              fontSize: 21,
              fontWeight: 700,
              letterSpacing: 2.4,
              color: COLORS.textDim,
              textTransform: "uppercase",
              marginBottom: 20,
            }}
          >
            ¿Dónde estás configurando Neexy?
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            {TARGETS.map(({ Icon, label, active }, i) => (
              <div
                key={label}
                style={{
                  ...fadeUp(frame, 12 + i * 7, 24),
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 12,
                  padding: "24px 14px",
                  borderRadius: RADIUS.chip,
                  background: active ? COLORS.primarySoft : GRADIENTS.card,
                  border: `1.5px solid ${active ? COLORS.primary : COLORS.border}`,
                  boxShadow: active ? SHADOW.lift : "none",
                  minHeight: 148,
                  justifyContent: "center",
                }}
              >
                <Icon size={38} color={active ? COLORS.primary : COLORS.textMuted} />
                <div
                  style={{
                    fontSize: 20,
                    fontWeight: 600,
                    textAlign: "center",
                    lineHeight: 1.3,
                    whiteSpace: "pre-line",
                    color: active ? COLORS.text : COLORS.textMuted,
                  }}
                >
                  {label}
                </div>
              </div>
            ))}
          </div>

          <p
            style={{
              ...fadeUp(frame, 44, 22),
              marginTop: 24,
              fontSize: 23,
              lineHeight: 1.45,
              color: COLORS.textDim,
            }}
          >
            En la tele no tendrás que escribir tu contraseña con el control.
          </p>
        </div>

        {/* Los tres pasos */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 20 }}>
          <Paso n={1} frame={frame} delay={20} accent>
            <div style={{ marginBottom: 16 }}>
              Abre la app que instalaste. Cuando te pida el <strong>servidor</strong>,
              escribe esta dirección:
            </div>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 18,
                padding: "16px 30px",
                borderRadius: 14,
                background: COLORS.bg,
                border: `1.5px solid rgba(254,65,85,0.5)`,
                boxShadow: `0 0 ${addrGlow * 44}px rgba(254,65,85,${addrGlow * 0.32})`,
              }}
            >
              <span
                style={{
                  fontFamily: monoFont,
                  fontSize: 40,
                  fontWeight: 700,
                  color: COLORS.primary,
                  letterSpacing: -0.4,
                }}
              >
                tv.neexy.net
              </span>
            </div>
            <div style={{ marginTop: 14, fontSize: 22, color: COLORS.textDim }}>
              Solo la escribes una vez, la app la recuerda.
            </div>
          </Paso>

          <Paso n={2} frame={frame} delay={PASO2_DELAY}>
            En la pantalla de inicio de sesión elige{" "}
            <strong style={{ color: COLORS.primary }}>Quick Connect</strong>. Tu pantalla
            mostrará un código.
          </Paso>

          <Paso n={3} frame={frame} delay={PASO3_DELAY}>
            <div style={{ marginBottom: 18 }}>
              Escribe ese código aquí y listo — sin usuario ni contraseña.
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
              <div
                style={{
                  display: "flex",
                  gap: 10,
                  fontFamily: monoFont,
                }}
              >
                {CODE.split("").map((c, i) => (
                  <div
                    key={i}
                    style={{
                      width: 58,
                      height: 70,
                      borderRadius: 12,
                      background: COLORS.bg,
                      border: `1.5px solid ${i < typed ? COLORS.primary : COLORS.border}`,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 34,
                      fontWeight: 700,
                      color: i < typed ? COLORS.text : COLORS.textDim,
                    }}
                  >
                    {i < typed ? c : "0"}
                  </div>
                ))}
              </div>
              <div
                style={{
                  opacity: typed === CODE.length ? 1 : 0.4,
                  padding: "18px 40px",
                  borderRadius: 14,
                  background: GRADIENTS.accentPill,
                  fontSize: 27,
                  fontWeight: 700,
                  color: "#fff",
                }}
              >
                Conectar
              </div>
            </div>
          </Paso>
        </div>
      </div>
    </Stage>
  );
};

interface PasoProps {
  n: number;
  frame: number;
  delay: number;
  accent?: boolean;
  children: React.ReactNode;
}

const Paso: React.FC<PasoProps> = ({ n, frame, delay, accent = false, children }) => (
  <div
    style={{
      ...fadeUp(frame, delay, 30),
      display: "flex",
      gap: 22,
      padding: "24px 28px",
      borderRadius: RADIUS.card,
      background: accent ? COLORS.primarySoft : GRADIENTS.card,
      border: `1px solid ${accent ? "rgba(254,65,85,0.3)" : COLORS.border}`,
    }}
  >
    <div
      style={{
        flexShrink: 0,
        width: 48,
        height: 48,
        borderRadius: RADIUS.pill,
        background: GRADIENTS.accentPill,
        color: "#fff",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 25,
        fontWeight: 800,
      }}
    >
      {n}
    </div>
    <div style={{ fontSize: TEXT.body, lineHeight: 1.4, flex: 1 }}>{children}</div>
  </div>
);

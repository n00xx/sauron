import React from "react";
import { useCurrentFrame } from "remotion";
import { Shot } from "../../components/Shots";
import { IconLaptop, IconPhone, IconTablet, IconTv } from "../../components/Icons";
import { COLORS, GRADIENTS, RADIUS, SHADOW } from "../../styles";
import { fadeUp, sweep } from "../../anim";
import { StepsScene, VISUAL_H, VISUAL_W } from "./StepsScene";

/**
 * Tiempos medidos sobre la locucion real con:
 *   ffmpeg -i public/voiceover/antes/04-dispositivos.mp3 -af silencedetect=n=-32dB:d=0.25 -f null -
 *
 * El audio arranca en el frame 12 (delay de 0.4s):
 *   "Si ya los usas todos y quieres ver Neexy en otro" -> audio  3.17s -> frame 107
 *   "primero cierra sesion"                            -> audio  6.33s -> frame 202
 *   "toca tu inicial"                                  -> audio  9.48s -> frame 296
 *   "y luego Sign Out"                                 -> audio 12.24s -> frame 379
 */
const OTRO = 95;
const CIERRA = 190;
const INICIAL = 284;
const SIGNOUT = 367;

const fit = { maxW: VISUAL_W, maxH: VISUAL_H };

type Tone = "on" | "off" | "wait";

const TONES: Record<Tone, { text: string; color: string }> = {
  on: { text: "En uso", color: COLORS.success },
  off: { text: "Sesión cerrada", color: COLORS.textDim },
  wait: { text: "Sin lugar", color: COLORS.warning },
};

const SlotRow: React.FC<{
  Icon: React.FC<{ size?: number; color?: string }>;
  label: string;
  tone: Tone;
  dim?: number;
}> = ({ Icon, label, tone, dim = 0 }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 18, opacity: 1 - dim * 0.55 }}>
    <div
      style={{
        width: 56,
        height: 56,
        borderRadius: RADIUS.pill,
        background: COLORS.primarySoft,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Icon size={30} color={COLORS.primary} />
    </div>
    <span style={{ flex: 1, fontSize: 28, fontWeight: 600 }}>{label}</span>
    <span style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 22, fontWeight: 700, color: TONES[tone].color }}>
      <span style={{ width: 12, height: 12, borderRadius: 999, background: TONES[tone].color }} />
      {TONES[tone].text}
    </span>
  </div>
);

/**
 * El ejemplo del texto original: quiere ver en el celular en lugar de la Smart
 * TV. Se cierra la sesion en la tele y el celular toma su lugar.
 */
const Slots: React.FC = () => {
  const frame = useCurrentFrame();
  const closed = frame >= CIERRA;

  return (
    <div
      style={{
        width: 620,
        display: "flex",
        flexDirection: "column",
        gap: 20,
        padding: "30px 34px",
        borderRadius: RADIUS.card,
        background: GRADIENTS.card,
        border: `1px solid ${COLORS.border}`,
        boxShadow: SHADOW.card,
      }}
    >
      <div style={{ fontSize: 20, fontWeight: 700, letterSpacing: 2, textTransform: "uppercase", color: COLORS.textDim }}>
        Dispositivos de tu plan
      </div>
      <SlotRow Icon={IconTv} label="Smart TV" tone={closed ? "off" : "on"} dim={sweep(frame, CIERRA, 12)} />
      <SlotRow Icon={IconLaptop} label="Laptop" tone="on" />
      <SlotRow Icon={IconTablet} label="Tablet" tone="on" />
      <div style={{ ...fadeUp(frame, OTRO, 20), borderTop: `1px dashed ${COLORS.borderHi}`, paddingTop: 20 }}>
        <SlotRow Icon={IconPhone} label="Celular" tone={closed ? "on" : "wait"} />
      </div>
    </div>
  );
};

export const SceneOtroDispositivo: React.FC = () => (
  <StepsScene
    eyebrow="Cambiar de dispositivo"
    title="¿Quieres ver Neexy en otro dispositivo?"
    glowX="44%"
    note={{
      text: "Tu plan incluye cierto número de dispositivos. Si ya los usas todos, libera uno:",
      at: 14,
    }}
    steps={[
      { text: "Cierra sesión en el que ya no vas a usar", at: CIERRA },
      { text: "Toca tu inicial, arriba a la izquierda", at: INICIAL },
      { text: <>Toca <strong>Sign Out</strong></>, at: SIGNOUT },
    ]}
    visuals={[
      { from: 10, node: <Slots /> },
      {
        from: INICIAL,
        node: <Shot src="img/antes/cerrar-sesion-inicial.webp" w={726} h={231} {...fit} />,
      },
      {
        from: SIGNOUT,
        node: <Shot src="img/antes/cerrar-sesion-sign-out.webp" w={732} h={480} {...fit} />,
      },
    ]}
  />
);

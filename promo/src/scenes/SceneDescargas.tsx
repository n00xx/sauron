import React from "react";
import { useCurrentFrame, interpolateColors } from "remotion";
import { Stage } from "../components/Stage";
import { COLORS, GRADIENTS, RADIUS, SHADOW, TEXT } from "../styles";
import { monoFont } from "../fonts";
import { fadeUp, popIn, sweep } from "../anim";
import {
  IconPhone,
  IconTablet,
  IconLaptop,
  IconStore,
  IconDownload,
} from "../components/Icons";

/**
 * Tiempos medidos sobre la locucion real con:
 *   ffmpeg -i public/voiceover/06-descargas.mp3 -af silencedetect=n=-32dB:d=0.25 -f null -
 *
 * El audio arranca en el frame 12 (delay de 0.4s), asi que cada tienda se
 * enciende ~0.4s antes de que la voz la nombre:
 *   "descarga Moonfin"       -> audio  2.63s -> frame  91
 *   "Play Store en Android"  -> audio  5.72s -> frame 184
 *   "o App Store en iPhone"  -> audio  7.34s -> frame 232
 *   "En Windows"             -> audio 10.62s -> frame 331
 */
const MOONFIN_DELAY = 79;
const PLAY_DELAY = 172;
const APPLE_DELAY = 220;
const WINDOWS_DELAY = 319;

const LIT_BORDER = "rgba(254, 65, 85, 0.5)";

interface StoreCardData {
  devices: React.FC<{ size?: number; color?: string }>[];
  label: string;
  store: string;
  lightAt: number;
  /** Windows no sale de una tienda sino de una pagina: se escribe como direccion. */
  isUrl?: boolean;
}

/**
 * Moonfin esta en la Play Store y en la App Store (iPhone, iPad y Mac), pero no
 * en la Microsoft Store: en Windows solo hay instalador y winget. Por eso la
 * tarjeta de Windows manda a la pagina de descarga y las otras dos a su tienda.
 */
const CARDS: StoreCardData[] = [
  {
    devices: [IconPhone, IconTablet],
    label: "Android",
    store: "Play Store",
    lightAt: PLAY_DELAY,
  },
  {
    devices: [IconPhone, IconTablet, IconLaptop],
    label: "iPhone · iPad · Mac",
    store: "App Store",
    lightAt: APPLE_DELAY,
  },
  {
    devices: [IconLaptop],
    label: "Windows",
    store: "neexy.net/descargar",
    lightAt: WINDOWS_DELAY,
    isUrl: true,
  },
];

/**
 * Las tres tarjetas estan en pantalla desde el principio con la tienda apagada,
 * para que el mapa completo se lea de un vistazo; la voz va encendiendo cada una.
 */
const StoreCard: React.FC<{ card: StoreCardData; index: number; frame: number }> = ({
  card,
  index,
  frame,
}) => {
  const lit = sweep(frame, card.lightAt, 14);
  const StoreIcon = card.isUrl ? IconDownload : IconStore;

  return (
    <div
      style={{
        ...fadeUp(frame, 22 + index * 9, 30),
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        borderRadius: RADIUS.card,
        background: GRADIENTS.card,
        border: `1.5px solid ${interpolateColors(lit, [0, 1], [COLORS.border, LIT_BORDER])}`,
        boxShadow: `${SHADOW.lift}, 0 0 ${lit * 48}px rgba(254,65,85,${lit * 0.22})`,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 18,
          padding: "34px 24px 28px",
        }}
      >
        <div style={{ display: "flex", gap: 12 }}>
          {card.devices.map((Icon, i) => (
            <div
              key={i}
              style={{
                width: 64,
                height: 64,
                borderRadius: RADIUS.pill,
                background: COLORS.primarySoft,
                border: `1px solid rgba(254,65,85,0.26)`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <Icon size={32} color={COLORS.primary} />
            </div>
          ))}
        </div>
        <div style={{ fontSize: TEXT.label, fontWeight: 700 }}>{card.label}</div>
      </div>

      <div
        style={{
          alignSelf: "stretch",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 14,
          padding: "26px 24px",
          background: "rgba(0, 0, 0, 0.18)",
          borderTop: `1px solid ${COLORS.border}`,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            opacity: 0.3 + lit * 0.7,
            transform: `scale(${popIn(frame, card.lightAt, 18)})`,
          }}
        >
          <StoreIcon
            size={34}
            color={interpolateColors(lit, [0, 1], [COLORS.textMuted, COLORS.primary])}
          />
          <span
            style={{
              fontFamily: card.isUrl ? monoFont : undefined,
              fontSize: card.isUrl ? 28 : 34,
              fontWeight: card.isUrl ? 700 : 800,
              letterSpacing: card.isUrl ? -0.5 : -0.3,
            }}
          >
            {card.store}
          </span>
        </div>
      </div>
    </div>
  );
};

export const SceneDescargas: React.FC = () => {
  const frame = useCurrentFrame();

  return (
    <Stage
      eyebrow="Celulares, tablets y computadoras"
      title="Descarga la app desde tu tienda"
      glowX="66%"
    >
      {/* Mismo sello que en las escenas de TV: la app no cambia, solo la tienda */}
      <div
        style={{
          ...fadeUp(frame, MOONFIN_DELAY, 26),
          marginTop: 40,
          display: "inline-flex",
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
        <span style={{ fontSize: 32, fontWeight: 800, color: "#fff" }}>Moonfin</span>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: 36,
          marginTop: 44,
          width: "100%",
          maxWidth: 1560,
        }}
      >
        {CARDS.map((card, i) => (
          <StoreCard key={card.label} card={card} index={i} frame={frame} />
        ))}
      </div>
    </Stage>
  );
};

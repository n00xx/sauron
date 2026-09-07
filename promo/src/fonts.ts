import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

// Inter es la que usa la web de Neexy; mantener la misma familia hace que el
// video se lea como parte del producto y no como una pieza aparte.
export const { fontFamily: uiFont } = loadInter("normal", {
  weights: ["400", "500", "600", "700", "800"],
  subsets: ["latin"],
  ignoreTooManyRequestsWarning: true,
});

// Mono solo para la direccion del servidor, que el usuario tiene que teclear.
export const { fontFamily: monoFont } = loadMono("normal", {
  weights: ["500", "700"],
  subsets: ["latin"],
  ignoreTooManyRequestsWarning: true,
});

# Video del wizard de Neexy

Video tutorial que reemplaza el texto de los pasos del wizard de onboarding
(el que ve el usuario después de crear su usuario y contraseña).

- **Salida**: `out/neexy-wizard.mp4` — 1920x1080, 30 fps, 92 s, H.264 + AAC
- **Hecho con**: [Remotion](https://remotion.dev) 4.x + React 19 + TypeScript
- **Voz**: ElevenLabs (`eleven_v3`, voz Bella `hpp4J3VqNfWAUOO0d1Us`), español

## Requisitos

- Node 20+ (usa `--env-file`, probado en Node 24)
- `ffmpeg` / `ffprobe` en el PATH
- `promo/.env` con `ELEVENLABS_API_KEY=...` (ignorado por git)

## Comandos

```bash
npm install
npm run studio      # preview interactivo en el navegador
npm run voiceover   # regenera los 8 MP3 con ElevenLabs (consume créditos)
npm run durations   # mide los MP3 con ffprobe
npm run render      # renderiza out/neexy-wizard.mp4
```

## Estructura

```
src/
├── index.ts             # registerRoot
├── Root.tsx             # una sola composición: WizardVideo (solo landscape)
├── WizardVideo.tsx      # TransitionSeries + capa global de audio
├── styles.ts            # paleta, tomada de app/static/css/main.css:263
├── fonts.ts             # Inter (UI) + JetBrains Mono (direcciones)
├── anim.ts              # helpers de animación compartidos
├── components/
│   ├── Stage.tsx        # fondo, halo, grano y encabezado común
│   └── Icons.tsx        # SVG en línea
└── scenes/              # 6 componentes → 8 escenas
                         # (SceneInstalar se reutiliza 3 veces)
```

## Las 8 escenas

| # | Escena | Duración | Contenido |
|---|--------|----------|-----------|
| 1 | Gracias | 9.5 s | Bienvenida y acceso al catálogo |
| 2 | Dispositivos | 9.5 s | "Mira donde quieras" — 4 categorías |
| 3 | Fire TV | 8 s | App **Wholphin** |
| 4 | Google TV | 9 s | App **Moonfin** |
| 5 | Roku | 8 s | App **Jellyfin** |
| 6 | Descargas | 13 s | Celulares, tablets, Windows y Mac → `neexy.net/descargar` |
| 7 | Conectar | 22.5 s | Servidor `tv.neexy.net` + Quick Connect |
| 8 | Cierre | 16 s | `neexy.net/blog` + "prepara las palomitas" |

Cada plataforma de TV usa una app distinta: Fire TV → Wholphin,
Google TV → Moonfin, Roku → Jellyfin. Las tres escenas comparten el
componente `SceneInstalar`, parametrizado por props.

El navegador no se menciona en ninguna escena: la vía de entrada que se enseña
es siempre la app instalada.

## Si cambias el guion

El orden importa: **la voz define las duraciones**, no al revés.

1. Edita los textos en `generate-voiceover.ts`
2. Borra el MP3 de las pistas que cambiaron — el script conserva las que ya
   existen para no gastar créditos de ElevenLabs de más
3. `npm run voiceover`
4. `npm run durations`
5. Actualiza el array `SCENES` en `src/WizardVideo.tsx` con
   `duración del audio + ~1.2 s` de aire por escena
6. `npm run render`

Cada escena debe durar al menos `audio + delay + 0.5 s`, o la locución se corta
en la transición.

## Capturas de origen

Las capturas viven en `public/img/`. Las de Fire TV y Roku venían de capturas
de pantalla y se les recortaron 25 px al pie para quitar la barra de estado del
sistema operativo que traían horneada:

| Archivo | Tamaño |
|---------|--------|
| `firetv-steps.png` | 991x674 |
| `googletv-steps.png` | 857x554 |
| `roku-steps.png` | 847x573 |

Se escalan ~1.5x para 1080p. Si se recapturan más grandes, basta con
reemplazar el archivo: el layout no cambia.

`googletv-steps.png` se derivó de `neexy.net/tutorials/firetv/google2.webp`
(capturas reales de Google TV) reemplazando las seis apariciones de "Jellyfin"
por "Moonfin". El script que hace el reemplazo mide la mancha de tinta de cada
instancia, la borra muestreando el fondo fila por fila y redibuja el texto en
Roboto ajustado al mismo cuerpo y color. Se conserva en
`scripts/moonfin-replace.py` por si hay que rehacerlo.

> **Nota:** en esa imagen el logotipo de la app sigue siendo el de Jellyfin
> (solo se cambió el texto). Si Moonfin tiene un logo propio, hay que
> regenerar la captura.

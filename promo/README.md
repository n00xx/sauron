# Videos del wizard de Neexy

Dos videos tutoriales que reemplazan el texto de los pasos del wizard de
onboarding (el que ve el usuario después de crear su usuario y contraseña):

| Composición | Paso del wizard | Salida | Duración |
|---|---|---|---|
| `AntesDeEmpezar` | 1 · "Algunas cosas que debes saber antes de empezar" | `out/neexy-antes-de-empezar.mp4` | 54.5 s |
| `WizardVideo` | 2 · "Ahora, instala la app" | `out/neexy-wizard.mp4` | 101 s |

Este README describe sobre todo `WizardVideo`; `AntesDeEmpezar` tiene su
sección al final.

- **Formato**: 1920x1080, 30 fps, H.264 + AAC
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
npm run voiceover   # genera los MP3 que falten con ElevenLabs (consume créditos)
npm run durations   # mide los MP3 con ffprobe
npm run render      # renderiza out/neexy-wizard.mp4
npm run render:antes   # renderiza out/neexy-antes-de-empezar.mp4
npm run poster:antes   # su portada (frame 250, las tres tarjetas encendidas)
```

Los MP4 que sirve la app son copias en `app/static/video/`: `out/` está
ignorado por git, así que después de renderizar hay que copiarlos ahí.

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
| 3 | Fire TV | 8.5 s | App **Moonfin** (Amazon Appstore) |
| 4 | Google TV | 7 s | App **Moonfin** |
| 5 | Roku | 7.5 s | App **Moonfin** (tienda de canales) |
| 6 | Descargas | 16 s | **Moonfin** en Play Store (Android) y App Store (iPhone, iPad, Mac); Windows → `neexy.net/descargar` |
| 7 | Conectar | 30.5 s | Servidor `tv.neexy.net` + Quick Connect + aviso de no cerrar la página |
| 8 | Cierre | 16 s | `neexy.net/blog` + "prepara las palomitas" |

Las tres plataformas de TV usan Moonfin (hasta septiembre de 2026 eran
Wholphin en Fire TV y Jellyfin en Roku). Las tres escenas comparten el
componente `SceneInstalar`, parametrizado por props: solo cambian la tienda y
la captura.

En celulares, tablets y computadoras también es Moonfin, desde la tienda de
cada sistema. Windows es la excepción: Moonfin no está en la Microsoft Store
(solo instalador de GitHub y `winget`), así que esa tarjeta sigue mandando a
`neexy.net/descargar`. Si algún día se publica ahí, basta con cambiar la
tarjeta en `SceneDescargas.tsx` y la frase final de `06-descargas`.

El aviso de la escena 7 no es decorativo: Quick Connect autoriza la tele contra
la cuenta que guardó la sesión del navegador al crearla
(`app/services/wizard_identity.py`). Si el comprador cierra la página, el
código de la tele ya no tiene a quién conectarse y ve "Your session expired".

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
6. Si cambió `07-conectar`, vuelve a medir las pausas con el comando
   `silencedetect` del comentario de `SceneConectar.tsx` y recalcula
   `PASO2_DELAY`, `PASO3_DELAY` y `AVISO_DELAY`: si no, los pasos aparecen
   desfasados de la voz. Lo mismo con `06-descargas`: el comando está en el
   comentario de `SceneDescargas.tsx`, y ahí se recalculan `MOONFIN_DELAY`,
   `PLAY_DELAY`, `APPLE_DELAY` y `WINDOWS_DELAY`, que encienden cada tienda
7. `npm run render`

Cada escena debe durar al menos `audio + delay + 0.5 s`, o la locución se corta
en la transición.

## Capturas de origen

Las capturas viven en `public/img/`:

| Archivo | Tamaño | Origen |
|---------|--------|--------|
| `firetv-steps.png` | 1536x1024 | `neexy.net/tutorials/firetv/fire.webp` |
| `googletv-steps.png` | 857x554 | `neexy.net/tutorials/firetv/google2.webp` |
| `roku-steps.png` | 847x573 | captura de pantalla, con 25 px recortados al pie (barra de estado del sistema) |

Se escalan ~1.5x para 1080p. Si se recapturan más grandes, basta con
reemplazar el archivo: el layout no cambia.

Las tres se derivaron de su original reemplazando cada "Jellyfin" por
"Moonfin" con `scripts/moonfin-replace.py`. El script mide la mancha de tinta
de cada instancia, la borra muestreando el fondo fila por fila y redibuja el
texto en Roboto ajustado al mismo cuerpo y color. Los rects de cada imagen
están en su `TARGETS`:

```bash
python3 scripts/moonfin-replace.py firetv fire.png firetv-steps.png --fonts fonts/
```

`--fonts` apunta a una carpeta con `Roboto-400/500/700` (.ttf o .woff; Google
Fonts sirve .woff).

> **Nota:** en las tres el logotipo de la app sigue siendo el triángulo de
> Jellyfin (solo se cambió el texto). Si Moonfin tiene un logo propio, hay que
> regenerar las capturas.

## Video del primer paso: `AntesDeEmpezar`

Reemplaza el texto con seis capturas de "Algunas cosas que debes saber antes de
empezar" (`wizard_steps/jellyfin/00_before_you_start.md`). Las capturas son las
mismas que usaba ese texto, copiadas de `app/static/img/wizard/` a
`public/img/antes/`, y la voz está en `public/voiceover/antes/`.

| # | Escena | Duración | Contenido |
|---|--------|----------|-----------|
| 1 | Intro | 10 s | Tres tarjetas: renovar, recuperar, cambiar de dispositivo |
| 2 | Renovar | 14.5 s | Enlace debajo del video → usuario y contraseña → Validar cuenta → plan |
| 3 | Recuperar | 11 s | `neexy.net` → Recuperar → usuario o contraseña → correo |
| 4 | Otro dispositivo | 15.5 s | Lugares del plan; cerrar sesión en la tele para entrar en el celular |
| 5 | Listo | 5.5 s | Puente al siguiente paso, el video de instalación |

Las escenas 2 a 4 comparten `scenes/antes/StepsScene.tsx`: pasos a la izquierda
(`components/StepList.tsx`) y capturas a la derecha que se suceden con un
fundido (`components/Shots.tsx`). Cada paso y cada captura entra en un frame
medido sobre su pista con `silencedetect`; el comando y los tiempos están en el
comentario de cada escena. Si cambia una pista, hay que volver a medirla.

La voz no dice la dirección de renovación: lleva un `?renovar=1` que al oído no
se entiende. Dice "el enlace que está debajo de este video", y el paso del
wizard lo pone como enlace debajo del reproductor.

En instalaciones que ya tenían el texto, `ensure_before_you_start_video()`
(`app/services/wizard_seed.py`) lo cambia por el video una sola vez, y solo si el
texto sigue idéntico al que se publicó; si el admin lo editó, lo deja y lo
avisa en el log.

"""
Reemplaza 'Jellyfin' por 'Moonfin' en google2.png (guia de Google TV de Neexy).

Metodo por instancia:
  1. Medir: dentro de un rect ajustado a mano (ver grid.png) se aisla la mancha
     de tinta y se extrae el color real del glifo segun sea texto morado o
     blanco, para no contaminarlo con las palabras vecinas.
  2. Borrar: se rellena fila por fila con fondo muestreado a la derecha del
     texto, asi se respetan los degradados verticales del panel.
  3. Redibujar: se busca el cuerpo de Roboto cuya mancha reproduce la original
     y se dibuja la cadena nueva alineada a la misma esquina superior-izquierda.

Alinear por bbox de tinta (y no por linea base) evita adivinar metricas de la
fuente. 'Moonfin' sale mas ancho que 'Jellyfin' al mismo cuerpo -- es correcto,
la M y las dos o ocupan mas que J-e-l-l-y.
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import statistics

# Fuente: https://neexy.net/tutorials/firetv/google2.webp (convertida a PNG)
SRC, DST = "google2.png", "googletv-moonfin.png"
FONT = {400: "Roboto-400.ttf", 500: "Roboto-500.ttf"}

# Rects verificados sobre grid.png. Cada uno encierra SOLO la palabra objetivo
# (en el panel 4 tambien las comillas, que son moradas y deben moverse con ella).
INSTANCES = [
    dict(name="4-titulo",    rect=(121, 292, 200, 318), old='"Jellyfin"', new='"Moonfin"',
         weight=500, kind="purple", bbox_mode="ink"),
    dict(name="4-buscador",  rect=( 58, 340, 112, 364), old="Jellyfin", new="Moonfin",
         weight=400, kind="white", cursor=True),
    dict(name="5-titulo",    rect=(404, 285, 478, 310), old="Jellyfin", new="Moonfin",
         weight=500, kind="purple", bbox_mode="kind"),
    dict(name="5-buscador",  rect=(334, 341, 392, 362), old="Jellyfin", new="Moonfin",
         weight=400, kind="white"),
    dict(name="5-resultado", rect=(377, 395, 440, 416), old="Jellyfin", new="Moonfin",
         weight=400, kind="white"),
    dict(name="6-titulo",    rect=(692, 364, 775, 393), old="Jellyfin", new="Moonfin",
         weight=400, kind="white"),
]

img = Image.open(SRC).convert("RGB")
px = img.load()


def measure(rect, kind, bbox_mode):
    x0, y0, x1, y1 = rect
    pixels = [px[x, y] for y in range(y0, y1) for x in range(x0, x1)]
    bg = tuple(int(statistics.median(c[i] for c in pixels)) for i in range(3))
    dist = lambda c: max(abs(c[i] - bg[i]) for i in range(3))

    ink = [(x, y) for y in range(y0, y1) for x in range(x0, x1) if dist(px[x, y]) > 55]
    if not ink:
        raise SystemExit(f"sin tinta en {rect}")

    # El morado se aisla por cuanto supera el azul al verde. Es imprescindible
    # para el bbox y no solo para el color: en los titulos la palabra va pegada
    # a una palabra blanca, y medir sobre toda la tinta se comeria esa vecina.
    if kind == "purple":
        sel = [p for p in ink if px[p[0], p[1]][2] - px[p[0], p[1]][1] > 25]
        rank = lambda p: px[p[0], p[1]][2] - px[p[0], p[1]][1]
    else:
        sel = ink
        rank = lambda p: sum(px[p[0], p[1]])
    sel = sel or ink

    # El bbox sale de toda la tinta cuando el rect ya aisla la palabra: en el
    # panel 4 las comillas son moradas pero mas claras, y el filtro las dejaria
    # fuera, con lo que quedarian huerfanas al redibujar.
    src = ink if bbox_mode == "ink" else sel
    xs, ys = [p[0] for p in src], [p[1] for p in src]
    bbox = (min(xs), min(ys), max(xs), max(ys))

    ordered = sorted(sel, key=rank, reverse=True)
    core = ordered[: max(1, len(ordered) // 4)]
    fg = tuple(int(sum(px[p[0], p[1]][i] for p in core) / len(core)) for i in range(3))
    return bbox, fg, bg


def ink_bbox(text, font):
    tmp = Image.new("L", (700, 220), 0)
    ImageDraw.Draw(tmp).text((60, 60), text, font=font, fill=255)
    return tmp.getbbox()


def fit_size(text, weight, target_w, target_h):
    """Cuerpo cuya mancha reproduce mejor la original. El alto pesa mas: fija
    la escala optica, mientras el ancho solo desempata."""
    best, best_err = None, None
    for size in range(6, 64):
        f = ImageFont.truetype(FONT[weight], size)
        b = ink_bbox(text, f)
        if not b:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        err = abs(h - target_h) * 3 + abs(w - target_w)
        if best_err is None or err < best_err:
            best, best_err = (size, f), err
    return best


draw = ImageDraw.Draw(img)

for inst in INSTANCES:
    bbox, fg, bg = measure(inst["rect"], inst["kind"], inst.get("bbox_mode", "kind"))
    bx0, by0, bx1, by1 = bbox
    ow, oh = bx1 - bx0 + 1, by1 - by0 + 1
    size, font = fit_size(inst["old"], inst["weight"], ow, oh)

    # --- borrar ---
    # Margen vertical de 1px: en el panel 5 la linea siguiente del titulo pasa
    # a 2px del descendente, y con mas margen se le corta la parte de arriba.
    erase_x1 = bx1 + (14 if inst.get("cursor") else 1)
    for y in range(by0 - 1, by1 + 2):
        if not 0 <= y < img.height:
            continue
        sx = min(erase_x1 + 5, img.width - 10)
        sample = [px[x, y] for x in range(sx, min(sx + 9, img.width))]
        row_bg = tuple(int(statistics.median(c[i] for c in sample)) for i in range(3)) if sample else bg
        for x in range(bx0 - 1, erase_x1 + 1):
            if 0 <= x < img.width:
                px[x, y] = row_bg

    # --- redibujar ---
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((bx0, by0), inst["new"], font=font, fill=fg + (255,))
    nb = layer.getbbox()
    layer = layer.transform(img.size, Image.AFFINE,
                            (1, 0, nb[0] - bx0, 0, 1, nb[1] - by0), resample=Image.BICUBIC)
    # El original es una captura suave; sin esto el texto nuevo canta por nitido
    layer = layer.filter(ImageFilter.GaussianBlur(0.35))
    img.paste(layer, (0, 0), layer)

    nw = nb[2] - nb[0]
    if inst.get("cursor"):
        cx = bx0 + nw + 3
        draw.rectangle([cx, by0, cx + 1, by1], fill=fg)

    print(f"{inst['name']:12s} ({bx0},{by0})-({bx1},{by1}) {ow}x{oh}  cuerpo={size}  "
          f"fg=#{fg[0]:02x}{fg[1]:02x}{fg[2]:02x}  nuevo={nw}px")

img.save(DST)
print(f"\nGuardado: {DST}")

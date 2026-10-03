# Tarjeta "Membresía vencida"

Portada de la biblioteca de Jellyfin que ve una cuenta en la pantalla de
renovación (`RENEWAL_LIBRARY_NAME` en `app/services/media/jellyfin.py`).
Se sube a mano como imagen Primary, Thumb y Backdrop de esa biblioteca.

- `tile.html` — la tarjeta, 1280×720, colores de `promo/src/styles.ts`.
- `qr.svg` — QR a `https://neexy.net/pay?renovar=1`, generado con `qr.js`
  (usa el codificador incluido en el paquete npm `qrcode-terminal`; ajustar
  la ruta del `require`).
- `membresia-vencida.png` — el render que está en tv.neexy.net.

Para regenerarla: abrir `tile.html` en Chromium a 1280×720 y capturar la
página. Comprobar el QR antes de subirla (en macOS, `CIDetector` de
CoreImage lo decodifica).

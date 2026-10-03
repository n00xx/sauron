// Prints an SVG QR code for the renewal URL, using the encoder vendored in qrcode-terminal.
const QRCode = require("/Users/mac/ClaudeProjects/petapp/node_modules/qrcode-terminal/vendor/QRCode");
const QRErrorCorrectLevel = require("/Users/mac/ClaudeProjects/petapp/node_modules/qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel");

const url = process.argv[2];
const qr = new QRCode(-1, QRErrorCorrectLevel.M);
qr.addData(url);
qr.make();
const n = qr.getModuleCount();
const quiet = 2;
let rects = "";
for (let r = 0; r < n; r++) {
  for (let c = 0; c < n; c++) {
    if (qr.isDark(r, c)) rects += `<rect x="${c + quiet}" y="${r + quiet}" width="1" height="1"/>`;
  }
}
const size = n + quiet * 2;
process.stdout.write(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges">` +
  `<rect width="${size}" height="${size}" fill="#ffffff"/><g fill="#0d121b">${rects}</g></svg>`
);

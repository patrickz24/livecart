// Draws the placeholder images of each brand, from its brand.json: no image
// service, no dependency, nothing but Node's zlib.
//
//   node scripts/generate-brand-assets.mjs            # every brand of brands/
//   node scripts/generate-brand-assets.mjs pomelune   # one brand
//
// Each image is the brand's mark, the initial of its display name in a disc,
// with an accent dot, in the brand's three colors; the files take the names
// of brand.json's "assets":
//
// - icon: 1024 px, opaque (iOS refuses transparent icons), mark on primary;
// - adaptiveIcon: 1024 px, transparent, the mark inside Android's safe zone
//   (app.config.ts sets the primary color behind it);
// - splash: 1024 px, transparent, shown on primary by expo-splash-screen;
// - logo: 256 px, transparent, shown by the app on its primary header.
//
// The output is deterministic: drawing again gives the same pixels. Replace
// the files with real artwork, under the same names, before any store build.
import { readFileSync, readdirSync, existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateSync } from "node:zlib";

const brandsDirectory = fileURLToPath(new URL("../brands/", import.meta.url));

// A 5x7 bitmap font, upper-case letters only: one string per row, 1 = ink.
const FONT = {
  A: ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
  B: ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
  C: ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
  D: ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
  E: ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
  F: ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
  G: ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
  H: ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
  I: ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
  J: ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
  K: ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
  L: ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
  M: ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
  N: ["10001", "10001", "11001", "10101", "10011", "10001", "10001"],
  O: ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
  P: ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
  Q: ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
  R: ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
  S: ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
  T: ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
  U: ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
  V: ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
  W: ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
  X: ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
  Y: ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
  Z: ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
};

/** [r, g, b, a] from #RRGGBB, opaque. */
export function parseColor(hex) {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!match) throw new Error(`${hex} is not a color as #RRGGBB`);
  return [...match.slice(1).map((pair) => Number.parseInt(pair, 16)), 255];
}

const TRANSPARENT = [0, 0, 0, 0];

const disc = (cx, cy, r, color) => ({
  color,
  contains: (x, y) => (x - cx) ** 2 + (y - cy) ** 2 <= r ** 2,
});

/** A letter of FONT, `height` tall, centered on (cx, cy); nothing for another character. */
function letter(char, cx, cy, height, color) {
  const rows = FONT[char];
  const cell = height / 7;
  const left = cx - (5 * cell) / 2;
  const top = cy - height / 2;
  return {
    color,
    contains: (x, y) => {
      const row = Math.floor((y - top) / cell);
      const column = Math.floor((x - left) / cell);
      return rows?.[row]?.[column] === "1";
    },
  };
}

/** The brand's mark: a disc of radius r (fractions of the image) with the initial and a dot. */
function mark(brand, r) {
  const [primary, onPrimary, accent] = ["primary", "onPrimary", "accent"].map((name) =>
    parseColor(brand.colors[name]),
  );
  const initial = brand.displayName.trim().charAt(0).toUpperCase();
  const offset = r * Math.SQRT1_2;
  return [
    disc(0.5, 0.5, r, onPrimary),
    letter(initial, 0.5, 0.5, r * 0.95, primary),
    disc(0.5 + offset, 0.5 + offset, r * 0.24, accent),
  ];
}

// Four samples a pixel: edges are antialiased.
const SAMPLES = [
  [0.25, 0.25],
  [0.75, 0.25],
  [0.25, 0.75],
  [0.75, 0.75],
];

/** RGBA pixels of a `size` square: `background`, then each shape painted over it. */
export function draw(size, background, shapes) {
  const pixels = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      let [r, g, b, a] = background.map((channel, index) =>
        index === 3 ? channel / 255 : channel,
      );
      for (const shape of shapes) {
        const hits = SAMPLES.filter(([dx, dy]) =>
          shape.contains((x + dx) / size, (y + dy) / size),
        ).length;
        if (hits === 0) continue;
        // Source over, straight alpha: the shape's coverage is its opacity.
        const alpha = hits / SAMPLES.length;
        const outAlpha = alpha + a * (1 - alpha);
        const blend = (source, destination) =>
          (source * alpha + destination * a * (1 - alpha)) / outAlpha;
        [r, g, b] = [blend(shape.color[0], r), blend(shape.color[1], g), blend(shape.color[2], b)];
        a = outAlpha;
      }
      pixels.set([r, g, b, a * 255].map(Math.round), (y * size + x) * 4);
    }
  }
  return pixels;
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

/** A PNG file of RGBA `pixels`: 8 bits a channel, every row unfiltered. */
export function encodePng(size, pixels) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header.set([8, 6, 0, 0, 0], 8); // bit depth 8, RGBA, deflate, no filter method, no interlace
  const rows = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y += 1) {
    pixels.copy(rows, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(rows, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** Every image of `brand`, as { file name: PNG bytes }. */
export function brandImages(brand) {
  const primary = parseColor(brand.colors.primary);
  const images = {
    icon: [1024, primary, mark(brand, 0.3)],
    // Android crops an adaptive icon to its central 66 %: the mark stays inside.
    adaptiveIcon: [1024, TRANSPARENT, mark(brand, 0.22)],
    splash: [1024, TRANSPARENT, mark(brand, 0.4)],
    logo: [256, TRANSPARENT, mark(brand, 0.46)],
  };
  return Object.fromEntries(
    Object.entries(images).map(([asset, [size, background, shapes]]) => [
      brand.assets[asset],
      encodePng(size, draw(size, background, shapes)),
    ]),
  );
}

function main(ids) {
  const brands =
    ids.length > 0
      ? ids
      : readdirSync(brandsDirectory).filter((id) =>
          existsSync(path.join(brandsDirectory, id, "brand.json")),
        );
  for (const id of brands) {
    const folder = path.join(brandsDirectory, id);
    const brand = JSON.parse(readFileSync(path.join(folder, "brand.json"), "utf8"));
    for (const [file, png] of Object.entries(brandImages(brand))) {
      writeFileSync(path.join(folder, file), png);
      console.log(`brands/${id}/${file}: ${String(png.length)} bytes`);
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2));
}

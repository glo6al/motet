const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const sharp = require('sharp');

const root = path.resolve(__dirname, '..');
const project = path.resolve(root, '../..');
const note = require('./geometry.json');
const lettering = require('./lettering.json');
const palettes = { charcoal: '#242424', white: '#ffffff', black: '#000000' };
const appSizes = [16, 24, 32, 48, 64, 128, 180, 192, 256, 512, 1024];
const faviconSizes = [16, 24, 32, 48, 64];
const macSizes = [16, 32, 128, 256, 512];

function svg(width, height, body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Motet"><title>Motet</title>${body}</svg>\n`;
}

function pathOutline(outline) {
  return `<path transform="${outline.transform}" fill-rule="evenodd" d="${outline.d}"/>`;
}

function outlineAt(outline, x, y, scale, color, paths) {
  return `<g fill="${color}" transform="translate(${x} ${y}) scale(${scale}) translate(${-outline.x} ${-outline.y})">${paths}</g>`;
}

function noteAt(x, y, width, color) {
  return outlineAt(note, x, y, width / note.width, color, pathOutline(note));
}

function markSvg(color) {
  const width = Math.ceil(note.width) + 48;
  const height = Math.ceil(note.height) + 48;
  return svg(width, height, noteAt((width - note.width) / 2, (height - note.height) / 2, note.width, color));
}

function wordSvg(color) {
  const width = Math.ceil(lettering.width) + 48;
  const height = Math.ceil(lettering.height) + 48;
  return svg(width, height, outlineAt(lettering, (width - lettering.width) / 2,
    (height - lettering.height) / 2, 1, color, lettering.paths.map(pathOutline).join('')));
}

function centeredNote(size, width, color) {
  const height = width * note.height / note.width;
  return noteAt((size - width) / 2, (size - height) / 2, width, color);
}

function tile(theme, mac = false) {
  const size = 1024;
  const dark = theme === 'dark';
  const inset = mac ? 64 : 0;
  const edge = size - 2 * inset;
  const background = dark ? palettes.charcoal : palettes.white;
  const foreground = dark ? palettes.white : palettes.charcoal;
  const stroke = mac && !dark ? ' stroke="#d8d8d8" stroke-width="2"' : '';
  return svg(size, size, `<rect x="${inset}" y="${inset}" width="${edge}" height="${edge}" rx="${mac ? 196 : 0}" fill="${background}"${stroke}/>`
    + centeredNote(size, mac ? 640 : 704, foreground));
}

function favicon(theme) {
  return svg(32, 32, centeredNote(32, 28, theme === 'dark' ? palettes.white : palettes.charcoal));
}

function save(relative, data) {
  const destination = path.join(root, relative);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, data);
}

async function png(source, relative, width) {
  const rendered = await sharp(Buffer.from(source)).resize(width).png().toBuffer();
  save(relative, rendered);
}

function ico(entries) {
  const header = Buffer.alloc(6);
  const table = Buffer.alloc(entries.length * 16);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(entries.length, 4);
  let offset = header.length + table.length;
  entries.forEach(({ size, data }, index) => {
    const row = index * 16;
    table[row] = table[row + 1] = size === 256 ? 0 : size;
    table.writeUInt16LE(1, row + 4);
    table.writeUInt16LE(32, row + 6);
    table.writeUInt32LE(data.length, row + 8);
    table.writeUInt32LE(offset, row + 12);
    offset += data.length;
  });
  return Buffer.concat([header, table, ...entries.map(({ data }) => data)]);
}

function writeIco(theme, kind, sizes) {
  save(`icons/${theme}/${kind}.ico`, ico(sizes.map(size => ({
    size,
    data: fs.readFileSync(path.join(root, `icons/${theme}/${kind}-${size}.png`)),
  }))));
}

function syncAssets() {
  const copies = {
    'src/web/static/icon.svg': 'svg/mark-charcoal.svg',
    'src/web/static/favicon.ico': 'icons/light/favicon.ico',
    'src/web/static/favicon-32.png': 'icons/light/favicon-32.png',
    'src/web/static/apple-touch-icon-180.png': 'icons/light/app-icon-180.png',
    'src/web/static/watch-lookout-mark.svg': 'svg/mark-charcoal.svg',
    'src/web/static/watch-lookout-lockup.svg': 'svg/watch-charcoal.svg',
    'src/web/static/watch-lookout-favicon.ico': 'icons/light/favicon.ico',
    'src/web/static/watch-lookout-favicon-32.png': 'icons/light/favicon-32.png',
    'src/web/static/watch-lookout-apple-touch-icon-180.png': 'icons/light/app-icon-180.png',
    'src/web/static/watch-lookout-favicon-light.svg': 'svg/favicon-light.svg',
    'src/web/static/watch-lookout-favicon-dark.svg': 'svg/favicon-dark.svg',
    'docs/media/logo-dark-green.svg': 'svg/watch-charcoal.svg',
    'docs/media/logo-graphite.svg': 'svg/watch-charcoal.svg',
    'docs/media/logo-green.svg': 'svg/watch-white.svg',
  };
  for (const [destination, source] of Object.entries(copies)) {
    fs.copyFileSync(path.join(root, source), path.join(project, destination));
  }
}

async function main() {
  for (const [name, color] of Object.entries(palettes)) {
    const shapes = [['mark', markSvg(color), 1088], ['watch', wordSvg(color), 1920]];
    for (const [kind, source, width] of shapes) {
      save(`svg/${kind}-${name}.svg`, source);
      await png(source, `png/${kind}-${name}.png`, width);
    }
  }

  for (const theme of ['light', 'dark']) {
    const app = tile(theme);
    const mac = tile(theme, true);
    const tab = favicon(theme);
    save(`svg/app-icon-${theme}.svg`, app);
    save(`svg/app-icon-macos-${theme}.svg`, mac);
    save(`svg/favicon-${theme}.svg`, tab);
    for (const size of appSizes) await png(app, `icons/${theme}/app-icon-${size}.png`, size);
    for (const size of faviconSizes) await png(tab, `icons/${theme}/favicon-${size}.png`, size);
    for (const size of macSizes) {
      const base = `icons/macos-${theme}.iconset/icon_${size}x${size}`;
      await png(mac, base + '.png', size);
      await png(mac, base + '@2x.png', size * 2);
    }
    writeIco(theme, 'favicon', [16, 32, 48, 64]);
    writeIco(theme, 'app-icon', [16, 32, 48, 64, 128, 256]);
    if (process.platform === 'darwin') {
      execFileSync('iconutil', ['-c', 'icns', path.join(root, `icons/macos-${theme}.iconset`),
        '-o', path.join(root, `icons/project-watch-${theme}.icns`)]);
    }
  }

  syncAssets();
  console.log('Built Motet artwork and synchronized the website and app assets.');
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

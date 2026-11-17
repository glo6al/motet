# Motet artwork

The wordmark uses outlined Avenir Next Bold lettering. The two t crossbars rise toward the right. The o uses an adaptation of the Bravura whole note, with its slanted opening. The note also serves as the standalone icon. The Avenir and Bravura font files are unnecessary to rebuild and are not distributed here. [Bravura source](https://github.com/steinbergmedia/bravura)

- `svg/watch-*.svg` and `png/watch-*.png` contain the wordmark in charcoal, white and black.
- `svg/mark-*.svg` and `png/mark-*.png` contain the single note in the same colors.
- `icons/light/` and `icons/dark/` contain square app icons at 16, 24, 32, 48, 64, 128, 180, 192, 256, 512 and 1024 pixels, plus app ICO files. They also contain transparent favicons at 16, 24, 32, 48 and 64 pixels and favicon ICO files.
- `icons/macos-*.iconset/` and `icons/project-watch-*.icns` contain the macOS tiles.
- `svg/favicon-light.svg` and `svg/favicon-dark.svg` provide browser icons for the two color schemes.

The artwork uses filled outlines with transparent openings. The filenames under `docs/media` and `src/web/static` are stable URLs that now serve Motet artwork.

## Rebuild

From the repository root, install the pinned build dependency and generate the assets.

```sh
npm install --prefix brand/lookout/source
npm run build --prefix brand/lookout/source
```

The generator reads `source/geometry.json` and `source/lettering.json`, writes the vectors and raster exports, and updates the app and README copies. On macOS it also builds the ICNS files with the system `iconutil`.

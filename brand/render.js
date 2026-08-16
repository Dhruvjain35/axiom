/**
 * Render a brand concept to PNG, at every size it has to survive.
 *
 * A logo is not one image. The same mark has to hold as a 1920x1280 Devpost card, as a
 * ~300px tile in a gallery grid beside three hundred other entries, and as a 32px favicon.
 * Marks that look good at the first size routinely dissolve at the third, and you cannot
 * tell which by looking at the big one.
 *
 * WHY THE SMALL SIZES ARE PRODUCED BY DOWNSCALING AND NOT BY RESIZING THE VIEWPORT.
 * The first version of this script made the tile by setting a 300x200 viewport and
 * screenshotting again. That does not shrink the design, it REFLOWS it — the body is a
 * fixed 1920x1280 canvas, so a 300px viewport just crops the top-left corner. The check
 * reported a gallery tile showing one enormous letter and nothing else, which was a fact
 * about the harness rather than about the logo. Downscaling the rendered card is what a
 * browser actually does to a thumbnail, so that is what is measured here.
 *
 *   node render.js concepts/foo.html   -> out/foo.png       3840x2560, the card at 2x
 *                                         out/foo@300.png   the gallery tile, downscaled
 *                                         out/foo@mark.png  the #mark element alone
 *                                         out/foo@32.png    the favicon test, downscaled
 *
 * 3:2 because Devpost crops submission thumbnails to 3:2, and anything else loses a band
 * the author did not choose.
 */
const {chromium} = require('/Users/dhruvjain/axiom-video/node_modules/playwright');
const {execFileSync} = require('child_process');
const path = require('path');
const fs = require('fs');

const CARD = {width: 1920, height: 1280};

/** macOS' own resampler. Same job a browser does when it paints an <img> smaller. */
const shrink = (src, dst, px) => {
  fs.copyFileSync(src, dst);
  execFileSync('sips', ['-Z', String(px), dst], {stdio: 'ignore'});
};

(async () => {
  const src = path.resolve(process.argv[2]);
  const slug = path.basename(src).replace(/\.html$/, '');
  const out = path.resolve(__dirname, 'out');
  fs.mkdirSync(out, {recursive: true});
  const at = (suffix) => path.join(out, `${slug}${suffix}.png`);

  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: CARD, deviceScaleFactor: 2});
  await page.goto('file://' + src);
  await page.waitForTimeout(450);

  await page.screenshot({path: at('')});
  const made = [at('')];

  shrink(at(''), at('@300'), 300);
  made.push(at('@300'));

  const mark = await page.$('#mark');
  if (mark) {
    await mark.screenshot({path: at('@mark')});
    shrink(at('@mark'), at('@32'), 32);
    made.push(at('@mark'), at('@32'));
  }

  await browser.close();
  console.log(made.join('\n'));
})();

// Magnify the favicon test so a human (or a model) can actually see the pixels.
// node zoom.js letter-a   ->  out/letter-a@32-zoom.png  (32px shown at 1x, 4x, 12x, on both grounds)
const {chromium} = require('/Users/dhruvjain/axiom-video/node_modules/playwright');
const path = require('path'), fs = require('fs');
(async () => {
  const slug = process.argv[2];
  const out = path.resolve(__dirname, 'out');
  const b64 = fs.readFileSync(path.join(out, `${slug}@32.png`)).toString('base64');
  const src = `data:image/png;base64,${b64}`;
  const html = `<style>
    body{margin:0;background:#7a7a7a;display:flex;align-items:center;gap:40px;padding:40px;
         font:12px Menlo;color:#111}
    img{image-rendering:pixelated;display:block}
    .c{display:flex;flex-direction:column;align-items:center;gap:10px}
    .w{background:#fff;padding:24px}
  </style>
  <div class=c><img src="${src}" width=32><span>32</span></div>
  <div class=c><img src="${src}" width=128><span>4x</span></div>
  <div class=c><img src="${src}" width=384><span>12x</span></div>
  <div class="c"><div class=w><img src="${src}" width=32></div><span>32 on white</span></div>`;
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport:{width:760,height:480}, deviceScaleFactor:2});
  await page.setContent(html);
  await page.waitForTimeout(200);
  await page.screenshot({path: path.join(out, `${slug}@32-zoom.png`)});
  await browser.close();
  console.log(path.join(out, `${slug}@32-zoom.png`));
})();

const { chromium } = require('playwright');
const path = require('path');

async function main() {
  const url = process.argv[2] || 'https://trading-bot-pro.vercel.app';
  const output = process.argv[3] || path.resolve('var', 'dashboard-shot.png');
  const edgePath = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

  const browser = await chromium.launch({
    headless: true,
    executablePath: edgePath,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 2200 }, deviceScaleFactor: 1 });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 120000 });
  await page.screenshot({ path: output, fullPage: true });
  await browser.close();
  console.log(output);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

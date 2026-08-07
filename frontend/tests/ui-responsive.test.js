/* Presentation-only regression checks for the Royal Blue light/dark redesign.
   Run against the same live stack as e2e.test.js. */
const { chromium } = require('playwright');
const os = require('os');
const path = require('path');

const APP = process.env.SNU_APP_URL || 'http://127.0.0.1:5173/index.html';
let pass = 0, fail = 0;
const ck = (name, ok, detail = '') => {
  console.log(`${ok ? '  PASS  ' : '  FAIL  '}${name}${detail ? `   [${detail}]` : ''}`);
  ok ? pass++ : fail++;
};

(async () => {
  const browser = await chromium.launch();
  const cases = [
    { name: 'laptop', viewport: { width: 1440, height: 960 } },
    { name: 'phone', viewport: { width: 390, height: 844 } },
  ];

  for (const testCase of cases) {
    console.log(`\n=== ${testCase.name.toUpperCase()} ${testCase.viewport.width}×${testCase.viewport.height} ===`);
    const page = await browser.newPage({ viewport: testCase.viewport });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(APP, { waitUntil: 'load' });
    await page.waitForTimeout(1800);

    for (const theme of ['light', 'dark']) {
      await page.click(`[data-theme-choice="${theme}"]`);
      const metrics = await page.evaluate(() => {
        const shell = document.querySelector('.workspace-shell');
        const active = document.querySelector('.pane.on');
        const themeButtons = [...document.querySelectorAll('.theme-choice')];
        return {
          theme: document.documentElement.dataset.theme,
          overflow: document.documentElement.scrollWidth - window.innerWidth,
          columns: getComputedStyle(shell).gridTemplateColumns,
          activeVisible: !!active && getComputedStyle(active).display !== 'none',
          minThemeTarget: Math.min(...themeButtons.map(b => Math.min(b.getBoundingClientRect().width, b.getBoundingClientRect().height))),
          navItems: document.querySelectorAll('#sectionNav .section-link').length,
        };
      });
      ck(`${theme} theme applies`, metrics.theme === theme, metrics.theme);
      ck(`${theme} has no page-level horizontal overflow`, metrics.overflow <= 1, `${metrics.overflow}px`);
      if (metrics.overflow > 1) {
        const offenders = await page.evaluate(() => [...document.querySelectorAll('body *')]
          .map(el => {
            const box = el.getBoundingClientRect();
            return { tag: el.tagName, id: el.id, cls: el.className, left: Math.round(box.left), right: Math.round(box.right), width: Math.round(box.width) };
          })
          .filter(box => box.right > window.innerWidth + 1 || box.left < -1)
          .sort((a, b) => b.right - a.right)
          .slice(0, 8));
        console.log(`  OVERFLOW  ${JSON.stringify(offenders)}`);
      }
      ck(`${theme} keeps an active workspace`, metrics.activeVisible);
      ck(`${theme} page map is populated`, metrics.navItems > 0, String(metrics.navItems));
      ck(`${theme} theme controls remain touchable`, metrics.minThemeTarget >= 36, `${metrics.minThemeTarget}px`);
      if (theme === 'dark') {
        const file = path.join(os.tmpdir(), `snu-ui-${testCase.name}-${theme}.png`);
        await page.screenshot({ path: file, fullPage: true });
        console.log(`  SNAP  ${file}`);
      }
    }

    const expectedSingleColumn = testCase.name === 'phone';
    const columns = await page.$eval('.workspace-shell', el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
    ck('responsive workspace column count', expectedSingleColumn ? columns === 1 : columns >= 2, String(columns));

    for (const tab of ['learn', 'prof', 'courses', 'bid', 'two', 'rules']) {
      await page.click(`.tab[data-p="${tab}"]`);
      const state = await page.evaluate(name => ({
        selected: document.querySelector(`.tab[data-p="${name}"]`).getAttribute('aria-selected'),
        visible: !document.getElementById(`p-${name}`).hidden,
      }), tab);
      ck(`${tab} tab remains interactive`, state.selected === 'true' && state.visible);
    }
    await page.click('.tab[data-p="prof"]');
    const profileLayout = await page.evaluate(() => {
      const pane = document.getElementById('p-prof');
      const widths = [...pane.querySelectorAll(':scope > .card')].map(card => Math.round(card.getBoundingClientRect().width));
      return { cards: widths.length, spread: Math.max(...widths) - Math.min(...widths), nav: document.querySelectorAll('#sectionNav .section-link').length };
    });
    ck('profile sections use the full readable width', profileLayout.cards === 6 && profileLayout.spread <= 1, `${profileLayout.cards} cards · ${profileLayout.spread}px spread`);
    ck('profile page map covers every major section', profileLayout.nav === 6, String(profileLayout.nav));

    await page.click('.tab[data-p="courses"]');
    const courseLayout = await page.evaluate(() => ({
      rows: document.querySelectorAll('#pickBody tr').length,
      innerOverflow: document.querySelector('.catalog-list').scrollWidth - document.querySelector('.catalog-list').clientWidth,
      view: document.getElementById('ttView').value,
      nav: document.querySelectorAll('#sectionNav .section-link').length,
    }));
    ck('course catalogue starts with a manageable page', courseLayout.rows <= 30, String(courseLayout.rows));
    ck('course catalogue does not require horizontal scrolling', courseLayout.innerOverflow <= 1, `${courseLayout.innerOverflow}px`);
    ck('readable timetable view is the default', courseLayout.view === 'agenda', courseLayout.view);
    ck('course page map covers every major tool', courseLayout.nav === 8, String(courseLayout.nav));
    await page.click('#sectionNav .section-link:last-child');
    ck('page map opens collapsed target sections', await page.$eval('#p-courses > details.more', el => el.open));

    await page.click('.tab[data-p="rules"]');
    await page.waitForSelector('.source-card');
    const sourceCount = await page.locator('.source-card').count();
    ck('rules group repeated citations into source cards', sourceCount >= 6, String(sourceCount));
    ck('no JavaScript errors', errors.length === 0, errors.slice(0, 2).join(' | '));
    await page.close();
  }

  await browser.close();
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
})().catch(e => { console.error(e); process.exit(1); });

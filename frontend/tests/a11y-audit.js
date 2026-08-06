/* Accessibility audit: axe-core injected into a real Chromium page via Playwright,
   run once per tab (not a static linter — this walks the actual live DOM after each
   tab's on-click draw functions have run, including the plan-toolbar controls on
   "Profile & budget"). Run via scripts/run-e2e.sh so backend + frontend are up. */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const APP = 'http://127.0.0.1:5173/index.html';
const AXE_SRC = fs.readFileSync(path.join(__dirname, '..', 'node_modules', 'axe-core', 'axe.min.js'), 'utf8');

const TABS = ['learn', 'prof', 'courses', 'bid', 'two', 'rules'];

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  await p.goto(APP, { waitUntil: 'load' });
  await p.waitForTimeout(2000);

  // populate enough state that data-dependent panes (stress, two, spec) have real
  // content rather than empty placeholders when axe scans them
  await p.evaluate(() => {
    PICK = {};
    ['CSD358', 'CSD361'].forEach(c => { if (BY[c]) PICK[c] = { want: 5, pkg: 0 }; });
    PRIO = { CSD358: 'MUST', CSD361: 'STRONG' };
    renderChosen();
  });
  await p.click('.tab[data-p="bid"]');
  await p.selectOption('#nsim', '1000');
  await p.click('#runBtn');
  await p.waitForFunction(() => window.RESULT && window.RESULT.recommendations, null, { timeout: 60000 });
  // stress-test now lives on the same "bid" pane, auto-triggered by runOpt() itself;
  // just wait for it rather than switching to a separate tab that no longer exists
  await p.waitForFunction(() => {
    const t = document.getElementById('stressOut').textContent;
    return t.length > 0 && !/Running synthetic cohorts/.test(t);
  }, null, { timeout: 30000 });

  // exercise the plan-toolbar controls once so their post-action state (messages,
  // populated picker) is present when axe scans the "Profile & budget" tab
  await p.click('.tab[data-p="prof"]');
  await p.fill('#planName', 'a11y audit plan');
  await p.click('button[onclick="void planSave()"]');
  await p.waitForTimeout(300);
  await p.click('button[onclick="void planDuplicate()"]');
  await p.waitForTimeout(300);

  let totalViolations = 0;
  const allResults = {};

  for (const tab of TABS) {
    await p.click(`.tab[data-p="${tab}"]`);
    await p.waitForTimeout(400);
    // open every collapsible <details class="more"> on this tab so its content is
    // actually part of the DOM axe scans, not silently skipped the way a hidden
    // .pane already is
    await p.evaluate(() => document.querySelectorAll('.pane.on details.more').forEach(d => d.open = true));
    await p.evaluate(AXE_SRC);
    const result = await p.evaluate(async () => {
      return await axe.run(document, {
        resultTypes: ['violations'],
        runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'] }
      });
    });
    // inactive panes are display:none (a_head.html: .pane{display:none}), which
    // axe already excludes from its scan, so result.violations here only reflects
    // the currently active pane plus always-visible chrome (header, tab bar).
    allResults[tab] = result.violations;
    totalViolations += result.violations.length;
    console.log(`\n=== tab: ${tab} (${result.violations.length} violation types) ===`);
    result.violations.forEach(v => {
      console.log(`  [${v.impact}] ${v.id}: ${v.help}`);
      v.nodes.slice(0, 5).forEach(n => console.log(`      ${n.target.join(' ')}`));
    });
  }

  fs.writeFileSync(path.join(__dirname, 'a11y-results.json'), JSON.stringify(allResults, null, 2));
  console.log(`\nTOTAL violation types across all tabs: ${totalViolations}`);
  await b.close();
  process.exit(totalViolations > 0 ? 1 : 0);
})();

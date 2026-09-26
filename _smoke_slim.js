const { chromium } = require('/home/fsptd/deepseek-harness/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/google/chrome/chrome', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && m.text().indexOf('bridge-sdk') < 0) errors.push('console: ' + m.text()); });
  await page.addInitScript(() => {
    window.AstrBotPluginPage = {
      ready: () => Promise.resolve(),
      apiGet: async (e) => {
        if (e === 'config') return { adminUsers: [], settings: {}, ui_state: {}, groups: [{ group_id: 'g1', platform_id: 'aiocqhttp', name: '集训群' }], options: {} };
        if (e === 'bindings') return { total: 1, items: [{ user_id: 'u1', platform: 'nowcoder', handle: '小粉兔PinkRabbit', platform_user_id: '163975402', qq_name: '小粉兔', verified_at: 1790000000, groups: [{ group_id: 'g1', manual: true }] }] };
        if (e === 'rank/members') return { group_id: 'g1', total: 1, manual_count: 1, items: [{ user_id: 'u1', qq_name: '小粉兔', manual: true, added_by: 'webui', added_at: 1790000000, preexisting: false, accounts: [{ platform: 'nowcoder', handle: '小粉兔PinkRabbit' }] }] };
        if (e === 'rank') return { rows: [], errors: [], stale: false };
        return {};
      },
      apiPost: async () => ({}),
    };
  });
  await page.goto('file:///home/fsptd/文档/ChatGPT/acmer_qq_group_bot/pages/settings/index.html');
  await page.waitForTimeout(800);
  const out = {};
  await page.locator('.nav-item[data-nav="bindings"]').click();
  await page.waitForTimeout(800);
  out.bindHeader = (await page.locator('section[data-view="bindings"] thead').innerText()).replace(/\s+/g, ' ').trim();
  out.bfGroupGone = await page.locator('#bfGroup').count() === 0;
  await page.locator('[data-edit-binding]').first().click();
  await page.waitForTimeout(300);
  out.drawerFields = (await page.locator('#drawer .field').allInnerTexts()).map(t => t.split('\n')[0]).join(' | ');
  out.identifierPrefilled = await page.locator('#bfIdentifier').inputValue();
  out.userReadonly = await page.locator('#bfUser').evaluate(n => n.readOnly);
  await page.locator('#bfCancel').click();
  await page.locator('.nav-item[data-nav="rank"]').click();
  await page.waitForTimeout(900);
  out.rankMemberCard = await page.locator('#rmBody').count() > 0 && await page.locator('#rmSearch').count() > 0;
  const rankText = await page.locator('section[data-view="rank"]').innerText();
  out.rankSaysSinglePlace = rankText.indexOf('成员归属统一在这里管理') >= 0;
  out.errors = errors;
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL: ' + e.message); process.exit(1); });

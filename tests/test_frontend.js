// Deterministic math and real Chart.js UI tests; no live Bloomberg connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { canonical, shifted, normalize, rolling, percentile, summary } = require('../static/rolling-change.js');
assert.equal(canonical('  aapl   us EQUITY '), 'AAPL US Equity');
assert.equal(canonical('<script>'), null);
assert.equal(canonical('BAC 6 PERP Pfd'), 'BAC 6 PERP Pfd');
assert.equal(canonical('USD 1M m-mkt'), 'USD 1M M-Mkt');
assert.equal(canonical('GNMA 30 Mtge'), 'GNMA 30 Mtge');
assert.equal(canonical('CITY 5 Muni'), 'CITY 5 Muni');
assert.equal(shifted('2024-03-31', '1m'), '2024-02-29');
assert.equal(shifted('2025-03-31', '1m'), '2025-02-28');
assert.equal(shifted('2024-02-29', '1y'), '2023-02-28');
const boundaryRows = [{date:'2025-01-03',value:100},{date:'2025-01-06',value:110},{date:'2026-01-05',value:120},{date:'2026-01-06',value:121}];
assert.deepEqual(summary(boundaryRows,'2026-01-05','2026-01-06','abs').periods[3], {label:'12M',latest:11,percentile:0,count:2});
const rows = normalize({ dates: ['2026-02-02', '2026-01-02', '2026-01-30', '2026-01-02', '2026-02-03', 'invalid'], values: [120, 90, 110, 100, null, 200] });
assert.deepEqual(rows.map(r => r.value), [100, 110, 120]);
assert.deepEqual(rolling(rows, '1m', 'abs'), [null, null, 20]);
assert.ok(Math.abs(rolling(rows, '1m', 'pct')[2] - 20) < 1e-10);
// Sunday 1 Feb maps to Friday 30 Jan; an insufficient lookback stays null.
const holidays = [{ date: '2026-01-30', value: 100 }, { date: '2026-03-01', value: 125 }];
assert.deepEqual(rolling(holidays, '1m', 'abs'), [null, 25]);
assert.deepEqual(rolling([{ date: '2026-01-01', value: 0 }, { date: '2026-02-01', value: 5 }], '1m', 'pct'), [null, null]);
assert.deepEqual(rolling([{ date: '2026-01-01', value: 0 }, { date: '2026-02-01', value: 5 }], '1m', 'abs'), [null, 5]);

assert.equal(percentile(1, [1, 2, 3]), 0);
assert.equal(percentile(3, [1, 2, 3]), 100);
assert.equal(percentile(2, [1, 2, 2, 3]), 50);
assert.equal(percentile(2, [2, 2, 2]), 50);
assert.equal(percentile(null, [1, 2]), null);
assert.equal(percentile(1, [null, 1]), null);
const sampleRows = [{date:'2025-01-01',value:100},{date:'2025-02-01',value:110},{date:'2025-03-01',value:99},{date:'2025-04-01',value:120}];
const snap = summary(sampleRows, '2025-02-01', '2025-03-31', 'abs');
assert.equal(snap.date, '2025-03-01');
assert.equal(snap.periods[0].latest, -11);
assert.equal(snap.periods[0].percentile, 0);
assert.equal(snap.periods[0].count, 2);
assert.equal(snap.periods[3].latest, null);
assert.equal(summary(sampleRows, '2026-01-01', '2026-02-01', 'pct').date, null);
assert.equal(summary([{date:'2025-01-01',value:100},{date:'2025-02-01',value:0},{date:'2025-03-01',value:5}], '', '2025-03-01', 'pct').periods[0].latest, null);

// Run: NODE_PATH=<runtime node_modules> node tests/test_frontend.js
// Actual standalone files, real Edge/Chart.js, loopback server, mocked APIs only.
async function ui() {
  const http = require('node:http');
  const { chromium } = require('playwright');
  const root = path.resolve(__dirname, '../static');
  const allowed = new Set(['index.html', 'rolling-change.js', 'rolling-change.css', 'chart.min.js']);
  const served = new Set();
  const server = http.createServer((req, res) => {
    const name = new URL(req.url, 'http://localhost').pathname.slice(1) || 'index.html';
    if (!allowed.has(name)) { res.writeHead(404); res.end(); return; }
    served.add(name);
    res.writeHead(200, {'Content-Type': name.endsWith('.js') ? 'application/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
    res.end(fs.readFileSync(path.join(root, name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const calls = [], searches = [], pageErrors = [], unexpected = [];
  let browser;
  let fail = false, refreshing = false, hold = false, catalogFail = false, stale = false, boundary = false;
  let heldHistory = [], heldCatalog = [];
  const fixture = { dates: [], last: [] };
  for (let d = new Date('2024-12-30T00:00:00Z'), i = 0; d <= new Date('2026-10-26T00:00:00Z'); d.setUTCDate(d.getUTCDate() + 1), i++) {
    if (![0, 6].includes(d.getUTCDay())) { fixture.dates.push(d.toISOString().slice(0, 10)); fixture.last.push(100 + i / 10); }
  }
  const catalog = [{ticker:'NVDA US Equity',name:'NVIDIA CORP'}, {ticker:'AAPL US Equity',name:'Apple Inc'}, {ticker:'700 HK Equity',name:'Tencent'}, {ticker:'SPX Index',name:'S&P 500'}];
  const reply = (route, body, status = 200) => route.fulfill({status, contentType:'application/json', body:JSON.stringify(body)});
  try {
    browser = await chromium.launch(process.platform === 'win32' ? {channel:'msedge', headless:true} : {headless:true});
    const context = await browser.newContext({viewport:{width:1440,height:1100}});
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) { unexpected.push(url.href); await route.abort(); return; }
      if (!url.pathname.startsWith('/api/')) { await route.continue(); return; }
      if (url.pathname === '/api/instruments') {
        searches.push(url.searchParams.get('q'));
        if (stale && url.searchParams.get('q') === 'late') {
          await new Promise(resolve => heldCatalog.push(resolve));
          await reply(route, {items:[{ticker:'LATE US Equity',name:'Old response'}]}).catch(() => {}); return;
        }
        if (catalogFail) { await reply(route, {error:'Catalog offline'},503); return; }
        const q = url.searchParams.get('q').toLowerCase();
        await reply(route, {items:catalog.filter(o => (o.ticker+' '+o.name).toLowerCase().includes(q))}); return;
      }
      if (url.pathname !== '/api/history') { unexpected.push(url.pathname); await route.abort(); return; }
      calls.push(url);
      const snapshot = { fail, refreshing, boundary };
      if (hold) await new Promise(resolve => heldHistory.push(resolve));
      if (snapshot.fail) { await reply(route,{error:'Provider offline'},503).catch(() => {}); return; }
      const data = {};
      for (const ticker of url.searchParams.get('tickers').split(',')) {
        if (ticker.startsWith('MISSING')) continue;
        const full = snapshot.boundary ? { dates:boundaryRows.map(r=>r.date), last:boundaryRows.map(r=>r.value) } : fixture;
        const from = url.searchParams.get('from'), to = url.searchParams.get('to');
        const rows = full.dates.map((date,i)=>({date,value:full.last[i]}));
        const visible = rows.filter(r=>r.date>=from && r.date<=to);
        const predecessor = rows.filter(r=>r.date<from).at(-1);
        if (url.searchParams.get('predecessor')==='1' && predecessor) visible.unshift(predecessor);
        data[ticker] = {dates:visible.map(r=>r.date),last:visible.map(r=>r.value)};
      }
      await reply(route,{ok:true,status:snapshot.refreshing && !url.searchParams.has('job') ? 'refreshing' : 'cached',job:'mock-job',data,errors:[]}).catch(() => {});
    });
    const page = await context.newPage();
    page.on('pageerror', e => pageErrors.push(e.message));
    await page.goto(origin);
    await page.waitForFunction(() => document.getElementById('rc-to').max.length > 0);
    const ready = () => page.waitForFunction(() => !document.getElementById('rc-run').disabled);
    const analyze = async () => { await page.click('#rc-run'); await ready(); };
    const charts = () => page.evaluate(() => ['price','first','second'].map(id => {
      const c = Chart.getChart(document.getElementById('rc-chart-'+id));
      return c ? {count:c.data.datasets.length,values:c.data.datasets[0].data,title:c.options.scales.y.title.text} : null;
    }));
    const paste = value => page.locator('#rc-search').evaluate((el,value) => {
      const data = new DataTransfer(); data.setData('text/plain', value);
      el.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
    },value);
    const add = async value => { await page.fill('#rc-search',value); await page.locator('#rc-suggestions:not([hidden])').waitFor(); await page.press('#rc-search','Enter'); };
    // Empty focus must not open saved SPX suggestions or query the server.
    await page.click('#rc-search'); await page.waitForTimeout(300);
    assert.equal(await page.locator('#rc-suggestions').isVisible(),false,'blank focus has no dropdown');
    assert.deepEqual(searches,[],'blank search makes no request');
    await page.fill('#rc-search','n'); await page.waitForTimeout(300);
    assert.equal(await page.locator('#rc-suggestions').isVisible(),false);
    assert.deepEqual(searches,[],'one-letter search makes no request');
    await page.fill('#rc-search','nvda');
    await page.getByRole('option',{name:/NVIDIA CORP.*NVDA US Equity/}).waitFor();
    await page.press('#rc-search','ArrowDown');
    await page.fill('#rc-search',''); await page.press('#rc-search','Enter');
    assert.deepEqual(pageErrors,[],'clearing a highlighted result is safe');
    await page.fill('#rc-search','NVIDIA CORP'); await page.waitForTimeout(350);
    await page.press('#rc-search','Enter');
    assert.equal(await page.getByRole('button',{name:'Remove NVDA US Equity',exact:true}).count(),1,'company name resolves to Bloomberg listing');
    assert.equal(await page.getByRole('button',{name:'Remove NVIDIA Corp',exact:true}).count(),0,'company name cannot fabricate a bond ticker');
    await page.click('#rc-clear');
    // Search help and long results fit the input on desktop and mobile.
    const checkSearchBox = async () => {
      const geometry = await page.evaluate(() => {
        const input = document.getElementById('rc-search'), list = document.getElementById('rc-suggestions');
        const a = input.getBoundingClientRect(), b = list.getBoundingClientRect();
        return {left:Math.abs(a.left-b.left),width:Math.abs(a.width-b.width),overflow:list.scrollWidth>list.clientWidth};
      });
      assert.equal(geometry.overflow,false,'ticker dropdown has no horizontal overflow');
      assert.ok(geometry.left<=1 && geometry.width<=1,'dropdown aligns with input');
    };
    await page.fill('#rc-search','nvda'); await page.getByRole('option',{name:/NVIDIA CORP.*NVDA US Equity/}).waitFor();
    await checkSearchBox();
    await page.setViewportSize({width:390,height:844}); await checkSearchBox();
    await page.setViewportSize({width:1440,height:1100});
    catalog.push({ticker:'LONGNAME US Equity',name:'An exceptionally long instrument description '.repeat(5)});
    await page.fill('#rc-search','LONGNAME');
    await page.getByRole('option',{name:/exceptionally long/}).waitFor();
    await checkSearchBox();
    await page.press('#rc-search','Escape');
    assert.equal(await page.locator('#rc-settings').isVisible(),true);
    assert.equal(await page.locator('[data-rc-price-scale=raw]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#rc-chart-first').isVisible(),false);
    await page.click('[data-rc-range="1m"]');
    assert.equal(await page.inputValue('#rc-from'),shifted(new Date().toISOString().slice(0,10),'1m'));
    await page.click('[data-rc-range="all"]');
    assert.equal(await page.inputValue('#rc-from'),'');

    // Debounced server name search and remembered-catalog fallback.
    const beforeSearch = searches.length;
    await page.fill('#rc-search','App'); await page.fill('#rc-search','Apple');
    await page.getByRole('option',{name:/Apple Inc.*AAPL/}).waitFor();
    assert.equal(searches.slice(beforeSearch).filter(q=>q==='App').length,0);
    catalogFail = true;
    await page.fill('#rc-search','Tencent'); await page.waitForTimeout(350);
    assert.equal(await page.getByRole('option',{name:/Tencent/}).count(),0);
    await page.fill('#rc-search','Apple'); await page.waitForTimeout(350);
    await page.getByRole('option',{name:/Apple Inc.*AAPL/}).click();
    catalogFail = false;
    await page.fill('#rc-search','Tencent');
    await page.getByRole('option',{name:/Tencent.*700 HK/}).waitFor();
    await page.press('#rc-search','ArrowDown'); await page.press('#rc-search','Enter');
    await paste('SPX Index; AAPL US Equity\nBAD <script>');
    assert.equal(await page.locator('.rc-chip').count(),2,'invalid pasted list is atomic');
    await paste('SPX Index; AAPL US Equity\n700 HK Equity');
    assert.equal(await page.locator('.rc-chip').count(),3,'paste canonicalizes and deduplicates');

    // Stale catalog responses cannot overwrite a newer query or reopen a closed list.
    stale = true; await page.fill('#rc-search','late');
    await page.waitForTimeout(300); assert.equal(heldCatalog.length,1);
    await page.fill('#rc-search','Apple');
    await page.waitForTimeout(300);
    heldCatalog.splice(0).forEach(r=>r()); await page.waitForTimeout(100);
    assert.equal(await page.getByRole('option',{name:/Old response/}).count(),0);
    await page.press('#rc-search','Escape');
    await page.waitForTimeout(250); assert.equal(await page.locator('#rc-suggestions').isVisible(),false);
    stale = false;

    await page.fill('#rc-from','2026-01-01'); await page.fill('#rc-to','2026-10-26');
    await analyze();
    assert.match(await page.textContent('#rc-status'),/Saved prices loaded/);
    assert.equal(calls[0].pathname,'/api/history');
    assert.equal(calls[0].searchParams.get('predecessor'),'1');
    assert.equal(calls[0].searchParams.get('from'),'2025-01-01');
    assert.equal(calls[0].searchParams.get('range'),'1');
    assert.equal(calls[0].searchParams.get('to'),'2026-10-26');
    let info = await charts(); assert.ok(info.every(c=>c.count===3));
    assert.equal(info[0].title,'Rebased (100)','multiple tickers default to rebased overlay');
    assert.ok(info[0].values.filter(v=>v!==null)[0]===100);
    const priceTooltip = await page.evaluate(()=>{
      const c=Chart.getChart(document.getElementById('rc-chart-price')),ds=c.data.datasets[0];
      const i=ds.data.findIndex(v=>v!==null);
      return c.options.plugins.tooltip.callbacks.label({dataset:ds,dataIndex:i,parsed:{y:ds.data[i]}});
    });
    assert.match(priceTooltip,/Price: 136\.7/,'scaled price tooltip retains the native price');
    const scaleFetches=calls.length, rollingBefore=info[1].values;
    await page.click('[data-rc-price-scale=pct]'); info=await charts();
    assert.equal(info[0].title,'Change from start (%)');
    assert.equal(info[0].values.find(v=>v!==null),0);
    await page.click('[data-rc-price-scale=zscore]'); info=await charts();
    assert.equal(info[0].title,'Z-score (SD)');
    assert.ok(info[0].values.some(v=>Number.isFinite(v)));
    assert.match(await page.textContent('#rc-price-note'),/60 closes/);
    await page.click('[data-rc-price-scale=raw]'); info=await charts();
    assert.equal(info[0].title,'Price (native units)');
    assert.equal(info[0].values.find(v=>v!==null),136.7);
    assert.deepEqual(info[1].values,rollingBefore,'price scales do not modify rolling changes');
    assert.equal(calls.length,scaleFetches,'scaling uses loaded prices without fetching');

    assert.ok(info[1].values.some(v=>v!==null));
    assert.equal(await page.locator('.rc-summary-table').count(),3);
    assert.equal(await page.locator('.rc-summary-table tbody tr').count(),12);
    assert.match(await page.locator('.rc-summary-table tbody td').first().textContent(),/%/);
    const beforeMode = calls.length;
    await page.selectOption('#rc-mode','abs'); info = await charts();
    assert.equal(info[1].title,'Change (price units)'); assert.equal(info[2].title,'Change (price units)');
    assert.equal((await page.locator('.rc-summary-table tbody td').first().textContent()).includes('%'),false);
    await page.selectOption('#rc-mode','pct'); info = await charts();
    assert.equal(info[1].title,'Change (%)'); assert.equal(info[0].title,'Price (native units)');
    assert.equal(calls.length,beforeMode,'mode changes reuse history');
    await page.selectOption('#rc-first','6m'); assert.ok((await charts()).every(c=>c===null));
    await analyze(); assert.equal(await page.textContent('#rc-title-first'),'Rolling 6 months change');

    // Missing history and provider failure keep data already displayed.
    await add('MISSING US Equity'); await analyze();
    assert.match(await page.textContent('#rc-status'),/No prices: MISSING US Equity/);
    assert.deepEqual(await page.locator('.rc-summary-table').last().locator('tbody td').allTextContents(),Array(8).fill('—'));
    fail = true; await analyze(); fail = false;
    assert.match(await page.textContent('#rc-status'),/503/);
    assert.ok((await charts())[0].values.some(v=>v!==null));

    // Cache-first job polling displays charts while still refreshing.
    refreshing = true; const beforePoll = calls.length; await page.click('#rc-run');
    await page.waitForFunction(() => document.getElementById('rc-status').textContent.includes('filling missing dates'));
    assert.ok((await charts())[0].values.some(v=>v!==null));
    await ready(); refreshing = false;
    assert.equal(calls.length-beforePoll,2); assert.equal(calls.at(-1).searchParams.get('job'),'mock-job');

    // Cancelling during the poll delay prevents the next job request.
    refreshing = true; const beforeCancelPoll = calls.length; await page.click('#rc-run');
    await page.waitForFunction(() => document.getElementById('rc-status').textContent.includes('filling missing dates'));
    await page.click('#rc-cancel'); await page.waitForTimeout(1650); refreshing = false;
    assert.equal(calls.length-beforeCancelPoll,1,'cancel removes the pending poll timer');
    assert.match(await page.textContent('#rc-status'),/cancelled/);

    // Explicit cancellation retains charts; clearing cancels and removes every result.
    hold = true; await page.click('#rc-run'); await page.waitForTimeout(100);
    assert.equal(heldHistory.length,1); await page.click('#rc-cancel');
    heldHistory.splice(0).forEach(r=>r()); await page.waitForTimeout(150);
    assert.match(await page.textContent('#rc-status'),/cancelled/);
    assert.ok((await charts())[0].values.some(v=>v!==null));
    await page.click('#rc-run'); await page.waitForTimeout(100); await page.click('#rc-clear');
    heldHistory.splice(0).forEach(r=>r()); hold = false; await page.waitForTimeout(150);
    assert.equal(await page.locator('.rc-chip').count(),0);
    assert.equal(await page.locator('.rc-summary-table').count(),0);
    assert.ok((await charts()).every(c=>c===null));
    assert.equal(await page.textContent('#rc-status'),'Select tickers to view rolling change analysis');

    // Browser regression for a holiday predecessor outside the nominal request start.
    boundary = true; await add('AAPL US Equity');
    await page.fill('#rc-from','2026-01-05'); await page.fill('#rc-to','2026-01-06');
    await page.selectOption('#rc-first','1m'); await page.selectOption('#rc-mode','abs'); await analyze();
    const twelveMonth = page.locator('.rc-summary-table tbody tr').nth(3);
    assert.deepEqual(await twelveMonth.locator('td').allTextContents(),['+11.0','0']);
    assert.match(await twelveMonth.locator('td').last().getAttribute('title'),/^2 valid observations/);
    assert.equal(calls.at(-1).searchParams.get('from'),'2025-01-05');
    boundary = false;

    // Reload restores only this app's namespace and all selected controls.
    await page.evaluate(()=>localStorage.setItem('rollingChange','{"selected":["OTHER US Equity"]}'));
    await page.reload(); await page.waitForFunction(()=>document.getElementById('rc-to').max.length>0);
    assert.equal(await page.locator('.rc-chip').count(),1);
    assert.match(await page.locator('.rc-chip').textContent(),/AAPL/);
    assert.equal(await page.inputValue('#rc-mode'),'abs');
    assert.equal(await page.locator('[data-rc-price-scale=raw]').getAttribute('aria-pressed'),'true','explicit scale survives reload');
    assert.equal(await page.inputValue('#rc-from'),'2026-01-05');
    assert.equal(await page.inputValue('#rc-to'),'2026-01-06');
    assert.equal(await page.inputValue('#rc-first'),'1m');
    await page.click('[data-rc-range="all"]'); await analyze();
    assert.equal((+new Date(calls.at(-1).searchParams.get('to'))-+new Date(calls.at(-1).searchParams.get('from')))/86400000,2600);
    if (process.env.RC_PREVIEW_DIR) {
      fs.mkdirSync(process.env.RC_PREVIEW_DIR,{recursive:true});
      await page.screenshot({path:path.join(process.env.RC_PREVIEW_DIR,'rolling-standalone-desktop.png'),fullPage:true});
    }
    await page.setViewportSize({width:390,height:844}); await page.waitForTimeout(200);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'mobile has no horizontal overflow');
    assert.equal(await page.locator('#rc-chart-first').isVisible(),true);
    if (process.env.RC_PREVIEW_DIR) await page.screenshot({path:path.join(process.env.RC_PREVIEW_DIR,'rolling-standalone-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1100});

    // 101 instruments split into bounded requests while sharing a single Analyze action.
    await page.click('#rc-clear');
    await paste(Array.from({length:101},(_,i)=>`BULK${i} US Equity`).join('\n'));
    assert.equal(await page.locator('.rc-chip').count(),101);
    const beforeBulk = calls.length; await analyze();
    assert.equal(calls.length-beforeBulk,2);
    assert.deepEqual(calls.slice(beforeBulk).map(u=>u.searchParams.get('tickers').split(',').length),[100,1]);
    assert.equal((await charts())[0].count,101);
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('jcap.rolling-analysis.settings.v1')).selected.length),101);
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('rollingChange')).selected[0]),'OTHER US Equity');
    assert.equal(await page.evaluate(()=>['HISTORY','jcapStore','_posTickerMatches'].some(key=>key in window)),false);
    assert.deepEqual(unexpected,[]); assert.deepEqual(pageErrors,[]);
    assert.deepEqual([...served].sort(),[...allowed].sort());
    assert.ok(calls.every(url=>url.searchParams.get('predecessor')==='1'));
    assert.equal(require('node:crypto').createHash('sha256').update(fs.readFileSync(path.join(root,'chart.min.js'))).digest('hex'),'d2af8974e95271638772e9e9524db5b9a6f58d6ec2d5d781400447b4a31c681e','v1168 vendored Chart.js and license preserved byte for byte');
    console.log('PASS: pure math incl. predecessor summary; Edge actual-file startup, debounced search, stale guards, catalog fallback, paste, 3 charts, modes, horizons, summary, missing prices, provider failure, cache-first polling, cancel/clear, mobile, local restore, 100+1 batching; no live API calls.');
  } finally {
    heldHistory.splice(0).forEach(r=>r()); heldCatalog.splice(0).forEach(r=>r());
    if (browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
}
ui().catch(error=>{console.error(error);process.exitCode=1;});

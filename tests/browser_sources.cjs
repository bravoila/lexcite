// Synthetic model / documents, with real jobs, HTTP, rendering and citations.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.LEXCITE_TEST_URL,output=path.resolve(process.env.LEXCITE_BROWSER_OUTPUT||'test-results/sources');fs.mkdirSync(output,{recursive:true});
let browser;const checks=[];
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:JSON.parse(process.env.CHROMIUM_ARGS||'[]')});
 const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.locator('#password').fill(process.env.LEXCITE_TEST_PASSWORD);await page.locator('#login-button').click();await page.locator('#workspace').waitFor();
 async function api(route,data){const r=await context.request.post(base+'/api'+route,{headers:{'X-Lexcite':'1'},data});assert(r.ok(),await r.text());return r.json();}
 await api('/settings',{provider:'custom',base_url:'http://localhost:9999',model:'source-browser-fixture',agent_rounds:1});
 const cid=(await api('/cases',{title:'刑法来源验收（合成测试）',event_date:'2026-09-25'})).id;await page.reload();await page.locator('#case-title').filter({hasText:'刑法来源验收'}).waitFor();
 async function ask(text){await page.locator('#online-research').selectOption('on');await page.locator('#question').fill(text);await page.locator('#send').click();await page.waitForFunction(()=>!document.querySelector('#send').disabled);return page.locator('.answer').last();}
 let answer=await ask('目录外法源测试');
 assert((await answer.locator('.source-gaps').innerText()).includes('尚未配置搜索服务'));assert(!(await answer.innerText()).includes('《《'));assert(!(await answer.innerText()).includes('或达到本次搜索上限'));checks.push('specific-search-setup-error-visible-in-answer');
 await answer.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'missing-search-service.png')});
 await answer.locator('[data-action="search-settings"]').click();assert(await page.locator('#settings-view').isVisible());assert(await page.locator('#search-provider').isVisible());checks.push('one-click-to-search-settings');
 await page.locator('[data-view="research"]').click();answer=await ask('刑法中过失致人死亡的依据是什么？');
 assert.equal(await answer.locator('.status-chip').innerText(),'已核对引用');assert.equal(await answer.locator('.source-card').count(),2);assert.equal(await answer.locator('.source-gaps').count(),0);assert(!(await answer.innerText()).includes('《《'));checks.push('criminal-catalog-works-without-search-key');
 await answer.locator('.citation-btn').first().click();assert((await page.locator('#citation-title').innerText()).startsWith('《中华人民共和国刑法》'));
 assert((await page.locator('#citation-meta').innerText()).includes('2024-03-01'));assert.equal(await page.locator('#citation-source-note a').count(),1);assert((await page.locator('#citation-source-note').innerText()).includes('核对修正案施行日期'));
 await page.screenshot({path:path.join(output,'criminal-citation.png')});await page.locator('#citation-dialog .close').click();checks.push('citation-has-independent-amendment-date-source');
 // Simulate old saved data with wrapped names; rendering must fix it without DB mutation.
 await page.route(base+'/api/cases/'+cid,async route=>{const response=await route.fetch();const value=await response.json();for(const m of value.messages){if(m.role==='assistant'){for(const g of m.payload.source_gaps||[])g.name='《'+g.name+'》';for(const c of m.payload.citations||[])c.name='《'+c.name+'》';}}await route.fulfill({response,contentType:'application/json',body:JSON.stringify(value)});});
 await page.reload();await page.locator('.answer').last().waitFor();assert(!(await page.locator('#messages').innerText()).includes('《《'));checks.push('legacy-wrapped-names-render-once');
 // A web-source URL with a javascript: scheme must render as plain text, never as a link; safe http(s) URLs keep their link.
 await page.route(base+'/api/cases/'+cid,async route=>{const response=await route.fetch();const value=await response.json();for(const m of value.messages){if(m.role==='assistant'){m.payload.web_sources=[{url:'javascript:alert(1)',title:'注入测试来源',note:'不应渲染为可点击链接',state:'clue'},{url:'https://example.com/x',title:'正常来源',note:'保留为普通链接',state:'clue'}];}}await route.fulfill({response,contentType:'application/json',body:JSON.stringify(value)});});
 await page.reload();await page.locator('.answer').last().waitFor();assert.equal(await page.locator('.answer').count(),2);assert.equal(await page.locator('.web-lead a[href^="javascript:"]').count(),0);for(const item of await page.locator('.answer').all()){await item.locator('.web-leads>summary').click();assert.equal(await item.locator('.web-lead button[data-import-url]').count(),1);assert((await item.locator('.web-leads').innerText()).includes('注入测试来源'));assert.equal(await item.locator('.web-lead a[href="https://example.com/x"]').count(),1);}checks.push('javascript-url-renders-as-plain-text');
 const exported=await context.request.get(base+'/api/cases/'+cid+'/export');assert((await exported.text()).includes('修正案日期依据'));assert(!(await exported.text()).includes('《《'));checks.push('export-retains-date-provenance');
 await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>document.querySelector('#sidebar').getBoundingClientRect().right<=1);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));checks.push('mobile-no-horizontal-overflow');
 assert.deepEqual(errors,[]);checks.push('no-javascript-errors');fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({browser:browser.version(),checks,synthetic:true},null,2));console.log(JSON.stringify({passed:checks.length,checks}));await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exit(1);});

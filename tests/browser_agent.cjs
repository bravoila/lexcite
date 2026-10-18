const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.LEXCITE_TEST_URL,password=process.env.LEXCITE_TEST_PASSWORD;
const output=path.resolve(process.env.LEXCITE_BROWSER_OUTPUT||'test-results/agent');fs.mkdirSync(output,{recursive:true});
let browser;const checks=[];
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:process.env.CHROMIUM_ARGS?JSON.parse(process.env.CHROMIUM_ARGS):[]});
 const context=await browser.newContext({viewport:{width:1280,height:900}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.locator('#password').fill(password);await page.locator('#login-button').click();await page.locator('#workspace').waitFor({state:'visible'});
 async function api(route,body,method='POST'){const response=await context.request.fetch(base+'/api'+route,{method,headers:{'X-Lexcite':'1'},...(body?{data:body}:{})});assert(response.ok(),await response.text());return response.json();}
 const law=await api('/example',undefined,'GET');const imported=await api('/laws',law);await api('/laws/'+imported.id+'/review',{confirmed:true,valid_from:'2021-01-01'});
 await api('/settings',{provider:'custom',base_url:'http://localhost:9999/v1',model:'agent-browser-fixture',thinking_mode:'auto'});
 const cid=(await api('/cases',{title:'研究 Agent 浏览器验收',event_date:'2025-01-01'})).id;
 await page.reload();await page.locator('#case-title').filter({hasText:'研究 Agent 浏览器验收'}).waitFor();
 await page.locator('#question').fill('未支付价款怎么办？');await page.locator('.web-options summary').click();await page.locator('#web-query').fill('浏览器公开检索测试');await page.locator('#send').click();
 await page.locator('#working').waitFor({state:'visible'});assert(await page.locator('#send').isDisabled());checks.push('job-running-visible');
 await page.reload();await page.locator('#working').waitFor({state:'visible'});assert(await page.locator('#send').isDisabled());checks.push('reload-resumes-real-job');const bounds=await page.locator('#working').boundingBox();assert(bounds.y>=0&&bounds.y+bounds.height<=900,'progress outside viewport');
 await page.getByText('制定修正方案',{exact:false}).first().waitFor({state:'attached'});checks.push('invalid-quote-enters-repair');
 await page.screenshot({path:path.join(output,'agent-progress.png'),fullPage:false});
 await page.locator('.claim').waitFor({state:'visible'});assert.equal(await page.locator('.message.user').count(),1);assert.equal(await page.locator('.answer').count(),1);checks.push('repaired-answer-saved-once');const exported=await context.request.get(base+'/api/cases/'+cid+'/export');assert((await exported.text()).includes('研究步骤'));checks.push('export-includes-agent-trace');
 await page.locator('.web-leads>summary').click();await page.locator('.web-lead').waitFor({state:'visible'});assert((await page.locator('.web-lead').innerText()).includes('已读取官网正文'));checks.push('web-lead-read-but-unreviewed');
 await page.locator('.citation-btn').first().click();await page.locator('#citation-dialog').waitFor({state:'visible'});assert((await page.locator('#citation-title').innerText()).includes('民法典'));await page.locator('#citation-dialog .close').click();checks.push('reviewed-citation-opens');
 await page.screenshot({path:path.join(output,'agent-result.png'),fullPage:false});
 await page.locator('[data-import-url]').click();await page.locator('#law-preview').waitFor({state:'visible'});assert(await page.locator('input[name="import-mode"][value="url"]').isChecked());assert((await page.locator('#law-text').inputValue()).includes('第一百四十一条'));await page.locator('#import-dialog .close').first().click();checks.push('online-lead-enters-import-review');
 await page.locator('#question').fill('未支付价款还能怎么处理？');if(!await page.locator('#web-query').isVisible())await page.locator('.web-options summary').click();await page.locator('#web-query').fill('');await page.locator('#send').click();await page.locator('#cancel-research').waitFor({state:'visible'});await page.locator('#cancel-research').click();await page.locator('#agent-current').filter({hasText:'研究已停止'}).waitFor();assert.equal(await page.locator('.answer').count(),1);checks.push('cancel-keeps-unfinished-answer-out');
 await page.locator('[data-view="settings"]').click();await page.waitForFunction(()=>document.querySelector('#model-name').value==='agent-browser-fixture');await page.locator('#thinking-mode').selectOption('enabled');await page.locator('#agent-rounds').fill('2');await page.locator('#settings-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#settings-result').textContent.includes('设置已保存'));assert.equal((await api('/settings',undefined,'GET')).thinking_mode,'enabled');checks.push('thinking-and-rounds-settings-save');
 await page.locator('#search-provider').selectOption('bocha');await page.locator('#search-key').fill('test-only-search-key');await page.locator('#search-settings-form button.primary').click();await page.waitForFunction(()=>document.querySelector('#search-key-state').textContent.includes('已保存'));assert.equal(await page.locator('#search-key').inputValue(),'');checks.push('separate-search-key-save');
 // Standalone tutorial: offline page, provider tabs, deep links, print & mobile.
 const guide=process.env.LEXCITE_GUIDE_PATH;if(guide){
  const tutorial=await context.newPage();tutorial.on('pageerror',e=>errors.push(e.message));await tutorial.goto('file://'+guide);
  assert(await tutorial.locator('#panel-aliyun').isVisible());assert(await tutorial.locator('#panel-deepseek').isHidden());
  await tutorial.locator('#tab-deepseek').click();assert(await tutorial.locator('#panel-deepseek').isVisible());assert(await tutorial.locator('#panel-aliyun').isHidden());checks.push('guide-provider-tabs');
  await tutorial.locator('#tab-deepseek').press('ArrowRight');assert(await tutorial.locator('#panel-siliconflow').isVisible());assert.equal(await tutorial.locator('#tab-siliconflow').getAttribute('aria-selected'),'true');checks.push('guide-keyboard-tabs');
  await tutorial.goto('file://'+guide+'#deepseek');assert(await tutorial.locator('#panel-deepseek').isVisible());checks.push('guide-deep-link-reveals-panel');
  await tutorial.locator('#cloud-model > .provider-tabs').scrollIntoViewIfNeeded();await tutorial.screenshot({path:path.join(output,'guide-tabs.png')});
  await tutorial.emulateMedia({media:'print'});for(const id of ['aliyun','deepseek','siliconflow'])assert(await tutorial.locator('#panel-'+id).isVisible());checks.push('guide-print-includes-all-providers');await tutorial.emulateMedia({media:'screen'});
  await tutorial.setViewportSize({width:390,height:844});await tutorial.locator('#tab-aliyun').click();await tutorial.locator('#cloud').scrollIntoViewIfNeeded();assert(await tutorial.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await tutorial.screenshot({path:path.join(output,'guide-mobile.png')});checks.push('guide-mobile-width');
 }
 assert.deepEqual(errors,[]);checks.push('no-javascript-errors');fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({browser:browser.version(),checks},null,2));console.log(JSON.stringify({passed:checks.length,checks}));
 await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exit(1);});

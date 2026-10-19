// Real Chromium layout + native dialog checks against a disposable API instance.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.LEXCITE_TEST_URL;
const password=process.env.LEXCITE_TEST_PASSWORD;
if(!base||!password)throw new Error('Use scripts/test_browser.py, not a production server.');
const output=path.resolve(process.env.LEXCITE_BROWSER_OUTPUT||'test-results/browser');
fs.mkdirSync(output,{recursive:true});
const checks=[];
let browser;
async function visibleFeedback(page,label){
 const box=page.locator('#import-feedback');await box.waitFor({state:'visible'});
 const geometry=await box.evaluate(node=>{const r=node.getBoundingClientRect();const hit=document.elementFromPoint(r.x+r.width/2,r.y+Math.min(20,r.height/2));return {top:r.top,bottom:r.bottom,height:innerHeight,inside:node.closest('dialog').matches(':modal'),onTop:hit===node||node.contains(hit),role:node.getAttribute('role')};});
 assert(geometry.top>=0&&geometry.bottom<=geometry.height&&geometry.inside&&geometry.onTop,JSON.stringify(geometry));
 assert.equal(geometry.role,'alert');
 assert(await page.locator('#toast').isHidden());checks.push(label);
}
async function openImport(page){await page.locator('[data-view="laws"]').click();await page.locator('#import-law').click();}
async function metadata(page,name){await page.locator('#law-name').fill(name);await page.locator('#law-version').fill('测试版');await page.locator('#law-from').fill('2021-01-01');}
(async()=>{
 const options={headless:true};
 if(process.env.CHROMIUM_EXECUTABLE)options.executablePath=process.env.CHROMIUM_EXECUTABLE;
 if(process.env.CHROMIUM_ARGS)options.args=JSON.parse(process.env.CHROMIUM_ARGS);
 browser=await chromium.launch(options);
 const context=await browser.newContext({viewport:{width:1280,height:800}});
 const page=await context.newPage();const errors=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>requests.push(r.url()));
 await page.goto(base);await page.locator('#password').fill(password);await page.locator('#login-button').click();await page.locator('#workspace').waitFor({state:'visible'});
 assert(requests.some(u=>u.endsWith('/assets/1.4.0/app.js')));assert(!requests.some(u=>u.endsWith('/static/app.js')));checks.push('versioned-assets-loaded');
 await openImport(page);await metadata(page,'浏览器解析测试');
 const invalid='这里没有正式法条标题，应当在弹窗内清楚显示读取错误。';
 await page.locator('#law-text').fill(invalid);await page.locator('#preview-law').click();
 await visibleFeedback(page,'desktop-error-visible-in-native-dialog');
 assert.equal(await page.locator('#law-text').inputValue(),invalid);
 await page.locator('#import-fields').evaluate(node=>node.scrollTop=node.scrollHeight);
 await visibleFeedback(page,'error-stays-visible-while-form-scrolls');
 await page.screenshot({path:path.join(output,'desktop-error.png')});
 await page.setViewportSize({width:390,height:844});await visibleFeedback(page,'mobile-error-visible-in-native-dialog');
 await page.screenshot({path:path.join(output,'mobile-error.png')});
 await page.setViewportSize({width:1280,height:800});
 const refText='第一百四十一条 本条仅为测试。\n第一百四十二条 本条依照本法\n第一百四十一条的规定办理。\n第一百四十三条 本条也是测试。';
 await page.locator('#law-text').fill(refText);await page.locator('#preview-law').click();
 await page.waitForFunction(()=>!document.querySelector('#import-submit').disabled);
 assert.match(await page.locator('#law-preview').textContent(),/共识别 3 条/);
 assert.match(await page.locator('#law-preview').textContent(),/交叉引用/);checks.push('wrapped-reference-preview-preserved');
 await page.locator('#import-dialog .close').first().click();
 await openImport(page);await page.locator('input[name="import-mode"][value="url"]').check();
 await page.locator('#law-url').fill('https://www.gov.cn/lexcite-browser-fixture.html');
 assert(await page.locator('#law-file').isDisabled());assert(await page.locator('#document-source').isHidden());
 // No file, no pasted text, no metadata, and no separate read-button click.
 const download=page.waitForResponse(r=>r.url().endsWith('/api/extract-url'));
 await page.locator('#preview-law').click();assert.equal((await download).status(),200);
 await page.waitForFunction(()=>!document.querySelector('#import-submit').disabled);
 assert.match(await page.locator('#law-text').inputValue(),/第一百四十一条的规定/);
 assert.match(await page.locator('#law-preview').textContent(),/共识别 3 条/);checks.push('url-only-auto-read-through-real-api');
 await metadata(page,'浏览器链接导入测试');await page.locator('#preview-law').click();
 await page.waitForFunction(()=>!document.querySelector('#import-submit').disabled);
 await page.screenshot({path:path.join(output,'url-preview.png')});
 await page.locator('#import-submit').click();await page.locator('#review-dialog').waitFor({state:'visible'});
 assert.equal(await page.locator('#review-source').getAttribute('href'),'https://www.gov.cn/lexcite-browser-fixture.html');
 assert.match(await page.locator('#review-articles').textContent(),/第一百四十一条的规定/);checks.push('url-only-import-and-review-provenance');
 await page.locator('#review-dialog .close').first().click();await openImport(page);
 await page.locator('input[name="import-mode"][value="url"]').check();await page.locator('#law-url').fill('https://www.gov.cn/unavailable.html');await page.locator('#preview-law').click();
 await visibleFeedback(page,'url-failure-visible-with-no-upload-requirement');assert.match(await page.locator('#import-feedback').textContent(),/HTTP 503/);
 assert(await page.locator('#document-source').isHidden());await page.locator('#import-dialog .close').first().click();
 // Detect mixed front/back versions instead of silently running incompatible code.
 await page.route('**/api/session',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({initialized:true,authenticated:false,version:'1.0.1'})}));
 await page.reload();await page.waitForFunction(()=>document.querySelector('#login-error').textContent.includes('版本不一致'));assert(await page.locator('#login-button').isDisabled());checks.push('mixed-version-error-visible');
 await page.unroute('**/api/session');
 await page.route(base+'/',async route=>{const response=await route.fetch();let html=await response.text();html=html.replace(' data-app-version="1.4.0"','').replace(/<p id="client-load-status"[\s\S]*?<\/p>/,'');await route.fulfill({response,body:html});});
 await page.reload();await page.waitForFunction(()=>document.querySelector('#login-error').textContent.includes('页面与脚本版本不一致'));assert(await page.locator('#login-button').isDisabled());checks.push('old-html-new-script-fails-clearly');
 assert.deepEqual(errors,[]);
 const result={passed:true,browser:browser.version(),checks,external_download:'fixture',actual_chromium_layout:true};
 fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exit(1);});

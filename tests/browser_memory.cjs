const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.LEXCITE_TEST_URL,output=path.resolve(process.env.LEXCITE_BROWSER_OUTPUT||'test-results/memory');fs.mkdirSync(output,{recursive:true});
let browser;const checks=[];
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:JSON.parse(process.env.CHROMIUM_ARGS||'[]')});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.locator('#password').fill(process.env.LEXCITE_TEST_PASSWORD);await page.locator('#login-button').click();await page.locator('#workspace').waitFor();
 async function api(route,data,method='POST'){const r=await context.request.fetch(base+'/api'+route,{method,headers:{'X-Lexcite':'1'},...(data?{data}:{})});assert(r.ok(),await r.text());return r.json();}
 const law=await api('/example',null,'GET'),lid=(await api('/laws',law)).id;await api('/laws/'+lid+'/review',{confirmed:true,valid_from:'2021-01-01'});
 const settings={provider:'custom',base_url:'http://localhost:9999/v1',model:'memory-browser-fixture',agent_rounds:1,show_reasoning:true,json_mode:'auto'};await api('/settings',settings);
 const cid=(await api('/cases',{title:'彩礼问题 · 信息记忆回归',event_date:'2025-01-01'})).id;
 // Persist the original question without an external LLM call.
 await api('/cases/'+cid+'/ask',{text:'离婚时彩礼能否返还？',mode:'extract'});await page.reload();
 const facts='彩礼给付以结婚为目的\n双方已办理结婚登记手续\n婚后共同生活\n彩礼是否导致给付方生活困难 未知';
 await page.locator('#question').fill(facts);await page.locator('#send').click();
 await page.locator('#reasoning-text').filter({hasText:'浏览器测试返回'}).waitFor();assert(await page.locator('#working').isVisible());checks.push('live-provider-reasoning-before-completion');
 const box=await page.locator('#working').boundingBox();assert(box.y>=0&&box.y+box.height<=1000);checks.push('reasoning-panel-visible');
 await page.screenshot({path:path.join(output,'reasoning-live.png')});
 await page.reload();await page.locator('#reasoning-text').filter({hasText:'浏览器测试返回'}).waitFor();checks.push('reload-retains-provider-reasoning');
 await page.locator('#send').waitFor();await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 let answer=page.locator('.answer').last();assert.equal(await answer.locator('.missing').count(),0);assert((await answer.locator('.unknown-facts').innerText()).includes('生活困难 未知'));checks.push('provided-facts-not-asked-again-and-unknown-retained');
 assert.equal(await page.locator('#observed-facts .observed-fact').count(),4);checks.push('fact-snapshot-visible');
 await answer.locator('.reasoning-history>summary').click();await answer.locator('.reasoning-entry>summary').first().click();assert((await answer.locator('.reasoning-text').first().innerText()).includes('<script>'));assert.equal(await page.evaluate(()=>window.reasoningXSS),undefined);checks.push('raw-reasoning-escaped-and-history-viewable');
 await answer.locator('.reasoning-history').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'reasoning-history.png')});
 await page.locator('#question').fill('之前不是告诉你了吗');await page.locator('#send').click();await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 answer=page.locator('.answer').last();assert.equal(await answer.locator('.missing').count(),0);assert((await answer.locator('.fact-snapshot').innerText()).includes('4 项'));const detail=await api('/cases/'+cid,null,'GET');assert.equal(detail.messages.at(-1).payload.research_focus,'离婚时彩礼能否返还？');checks.push('meta-followup-uses-original-case-context');
 await api('/settings',{...settings,model:'memory-browser-error'});await page.locator('#question').fill('继续分析');await page.locator('#send').click();await page.waitForFunction(()=>!document.querySelector('#send').disabled);answer=page.locator('.answer').last();assert.equal(await answer.locator('.status-chip').innerText(),'模型调用失败');assert((await answer.innerText()).includes('InvalidParameter'));checks.push('http400-is-model-error-with-vendor-detail');
 await page.locator('[data-view="settings"]').click();await page.locator('#json-mode').selectOption('prompt');await page.locator('#show-reasoning').uncheck();await page.locator('#settings-form button.primary').click();await page.waitForFunction(()=>document.querySelector('#settings-result').textContent.includes('设置已保存'));const saved=await api('/settings',null,'GET');assert.equal(saved.show_reasoning,false);assert.equal(saved.json_mode,'prompt');checks.push('reasoning-and-json-format-controls-save');
 await api('/settings',settings);await page.locator('[data-view="research"]').click();await page.setViewportSize({width:390,height:844});await page.locator('#question').fill('之前不是告诉你了吗');await page.locator('#send').click();await page.locator('#reasoning-text').filter({hasText:'浏览器测试返回'}).waitFor();await page.locator('#working').scrollIntoViewIfNeeded();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(output,'reasoning-mobile.png')});checks.push('mobile-reasoning-has-no-horizontal-overflow');await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 assert.deepEqual(errors,[]);checks.push('no-javascript-errors');fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({browser:browser.version(),checks},null,2));console.log(JSON.stringify({passed:checks.length,checks}));await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exit(1);});

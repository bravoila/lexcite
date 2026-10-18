// Browser data-flow checks use explicitly synthetic model and official text.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.LEXCITE_TEST_URL,output=path.resolve(process.env.LEXCITE_BROWSER_OUTPUT||'test-results/core');fs.mkdirSync(output,{recursive:true});
let browser;const checks=[];
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:JSON.parse(process.env.CHROMIUM_ARGS||'[]')});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.locator('#password').fill(process.env.LEXCITE_TEST_PASSWORD);await page.locator('#login-button').click();await page.locator('#workspace').waitFor();
 async function api(route,data,method='POST'){const r=await context.request.fetch(base+'/api'+route,{method,headers:{'X-Lexcite':'1'},...(data?{data}:{})});assert(r.ok(),await r.text());return r.json();}
 await api('/settings',{provider:'custom',base_url:'http://localhost:9999',model:'core-browser-fixture',agent_rounds:1,online_research:false});
 const cid=(await api('/cases',{title:'核心研究流程 · 合成测试',event_date:'2025-01-01'})).id;
 await page.reload();
 const question='离婚后，彩礼可以要求对方返还吗';
 async function ask(text){await page.locator('#question').fill(text);await page.locator('#send').click();await page.waitForFunction(()=>!document.querySelector('#send').disabled);return page.locator('.answer').last();}
 let answer=await ask(question);
 assert.equal(await answer.locator('.status-chip').innerText(),'缺少必要法源');assert((await answer.locator('.source-gaps').innerText()).includes('涉彩礼纠纷'));assert.equal(await answer.locator('.missing').count(),0);assert.equal(await answer.locator('.source-card').count(),0);checks.push('offline-missing-authority-not-fact-question-or-citation');
 await page.locator('#online-research').selectOption('on');answer=await ask(question);
 assert.equal(await answer.locator('.claim').count(),2);assert((await answer.innerText()).includes('对本问题的回答'));assert((await answer.innerText()).includes('结合本案事实'));assert((await answer.locator('.source-card').innerText()).includes('未人工复核'));checks.push('online-temporary-evidence-and-structured-answer');
 await answer.locator('.citation-btn').first().click();assert((await page.locator('#citation-meta').innerText()).includes('官网临时原文'));assert((await page.locator('#citation-source-note').innerText()).includes('条号连续性'));assert((await page.locator('#citation-body').innerText()).includes('测试条文'));await page.locator('#citation-dialog .close').click();checks.push('citation-opens-exact-body-and-source-checks');
 const followup='双方是否办理了结婚登记？办了。 离婚前是否共同生活？若共同生活，持续时间大约多久？共同生活一年。 是否存在因给付彩礼导致给付方生活困难的情形？没有 彩礼是否已用于共同生活或生育抚养子女？没有。';
 answer=await ask(followup);assert.equal(await page.locator('#observed-facts .observed-fact').count(),4);assert.equal(await answer.locator('.missing').count(),0);checks.push('exact-user-followup-four-facts-visible');
 await page.reload();await page.waitForFunction(()=>document.querySelectorAll('#observed-facts .observed-fact').length===4);assert.equal(await page.locator('#observed-facts .observed-fact').count(),4);const saved=await api('/cases/'+cid,null,'GET');assert.equal(saved.messages.at(-1).payload.research_focus,question);assert.equal((await api('/laws',null,'GET')).length,0);checks.push('reload-keeps-context-without-auto-import');
 const exported=await context.request.get(base+'/api/cases/'+cid+'/export');assert((await exported.text()).includes('来源级别：官网临时原文，未人工复核'));checks.push('export-retains-source-status');
 await page.locator('.answer').last().scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'core-answer-desktop.png')});
 await page.locator('[data-view="settings"]').click();await page.locator('#default-online-research').check();await page.locator('#settings-form button.primary').click();await page.waitForFunction(()=>document.querySelector('#settings-result').textContent.includes('设置已保存'));assert((await api('/settings',null,'GET')).online_research);checks.push('default-online-setting-saved');
 await page.locator('[data-view="research"]').click();await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.waitForFunction(()=>document.querySelector('#sidebar').getBoundingClientRect().right<=1);await page.locator('#question').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'core-answer-mobile.png')});checks.push('mobile-width-no-overflow');
 assert.deepEqual(errors,[]);checks.push('no-javascript-errors');fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({browser:browser.version(),checks,synthetic:true},null,2));console.log(JSON.stringify({passed:checks.length,checks}));await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exit(1);});

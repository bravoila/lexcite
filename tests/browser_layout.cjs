// Real Chromium geometry checks for the reported research-page layout defects.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.LEXCITE_TEST_URL,output=path.resolve(process.env.LEXCITE_BROWSER_OUTPUT||'test-results/layout');
fs.mkdirSync(output,{recursive:true});
let browser;const checks=[],measurements=[];
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:JSON.parse(process.env.CHROMIUM_ARGS||'[]')});
 const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 async function api(route,data,method='POST'){const r=await context.request.fetch(base+'/api'+route,{method,headers:{'X-Lexcite':'1'},...(data?{data}:{})});assert(r.ok(),await r.text());return r.json();}
 await page.goto(base);await page.locator('#password').fill(process.env.LEXCITE_TEST_PASSWORD);await page.locator('#login-button').click();await page.locator('#workspace').waitFor();
 await api('/settings',{provider:'custom',base_url:'http://localhost:9999',model:'core-browser-fixture',agent_rounds:1,online_research:false});
 await api('/cases',{title:'yunki',event_date:'2026-09-25'});await page.reload();
 await page.locator('#case-title').filter({hasText:'yunki'}).waitFor();
 await page.locator('#online-research').selectOption('on');
 await page.locator('#question').fill('离婚后，彩礼可以要求对方返还吗');await page.locator('#send').click();
 await page.waitForFunction(()=>!document.querySelector('#send').disabled);assert.equal(await page.locator('.claim').count(),2);
 await page.locator('#online-research').selectOption('default');
 async function geometry(label){
  const m=await page.evaluate(()=>{
   const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right};};
   const note=document.querySelector('#online-default-note'),r=note.getBoundingClientRect(),hit=document.elementFromPoint(r.x+5,r.y+r.height/2);
   return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,header:rect('.research-heading'),messages:rect('#messages'),composer:rect('#ask-form'),question:rect('#question'),source:rect('#online-research'),note:rect('#online-default-note'),send:rect('#send'),noteOnTop:hit===note||note.contains(hit)};
  });
  m.label=label;measurements.push(m);assert(m.scrollWidth<=m.width+1,JSON.stringify(m));
  assert(m.note.y>=m.source.bottom+3,JSON.stringify(m));assert(m.noteOnTop,JSON.stringify(m));
  assert(m.source.x>=m.composer.x&&m.source.right<=m.composer.right,JSON.stringify(m));
  return m;
 }
 for(const [width,height] of [[1920,1080],[1440,900],[1280,720],[1024,768],[951,600],[768,700],[390,844],[320,640]]){
  await page.setViewportSize({width,height});await page.locator('#ask-form').scrollIntoViewIfNeeded();
  if(width<=700)await page.waitForFunction(()=>document.querySelector('#sidebar').getBoundingClientRect().right<=1);
  await page.waitForFunction(()=>document.querySelector('#question').getBoundingClientRect().height<=40);
  const m=await geometry(width+'x'+height);
  assert(m.question.height>=35&&m.question.height<=40,JSON.stringify(m));
  assert(m.composer.height<205,JSON.stringify(m));
  if(width>=951){
   assert(m.composer.bottom<=height&&m.header.y>=0,JSON.stringify(m));
   assert(m.messages.height>=height*.45,JSON.stringify(m));
  }
  if(width>=1280)assert(m.header.height<=35,JSON.stringify(m));
  if(width>=1440)assert(m.messages.width>=width*.5,JSON.stringify(m));
  if([1440,390].includes(width))await page.screenshot({path:path.join(output,'research-'+width+'.png')});
  checks.push('no-overlap-and-compact-'+width+'x'+height);
 }
 await page.setViewportSize({width:1280,height:720});
 const longText=Array.from({length:25},(_,i)=>'补充事实第'+(i+1)+'行：这里保留完整的案件陈述。').join('\n');
 await page.locator('#question').fill(longText);
 const expanded=await page.locator('#question').evaluate(n=>({height:n.getBoundingClientRect().height,overflow:n.scrollHeight>n.clientHeight}));
 assert(expanded.height>80&&expanded.height<=113&&expanded.overflow);assert.equal(await page.locator('#question').inputValue(),longText);
 await geometry('long-question');await page.locator('#question').fill('');
 assert((await page.locator('#question').boundingBox()).height<=40);checks.push('long-question-grows-scrolls-without-truncation-and-shrinks');
 for(const [value,text] of [['off','本次仅使用本地'],['on','本次补查官方全文'],['default','默认仅使用本地']]){
  await page.locator('#online-research').selectOption(value);assert((await page.locator('#online-default-note').innerText()).includes(text));await geometry('source-'+value);
 }
 checks.push('source-description-follows-selection');
 await page.locator('.web-options summary').click();await page.locator('#web-query').fill('民法典');
 let m=await geometry('public-search-open');assert(m.send.bottom<=m.height&&m.composer.bottom<=m.height);assert(await page.locator('#web-query').isVisible());
 await page.locator('.web-options summary').click();checks.push('optional-public-search-expands-without-covering-description');
 // Long names stay readable via the full title; actions and composer still fit.
 await page.locator('#edit-case').click();
 await page.locator('#case-name-input').fill('一个用于验证较长案件名称在窄屏仍可使用的案件标题');
 await page.locator('#case-form button.primary').click();await page.locator('#case-dialog').waitFor({state:'hidden'});
 m=await geometry('long-case-title');assert(m.header.height<85&&m.composer.bottom<=m.height);checks.push('long-case-title-does-not-push-composer-offscreen');
 await page.locator('.citation-btn').first().click();assert(await page.locator('#citation-dialog').isVisible());await page.locator('#citation-dialog .close').click();checks.push('citations-remain-accessible');
 assert.deepEqual(errors,[]);checks.push('no-javascript-errors');
 fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({browser:browser.version(),checks,measurements,synthetic:true},null,2));console.log(JSON.stringify({passed:checks.length,checks}));await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exit(1);});

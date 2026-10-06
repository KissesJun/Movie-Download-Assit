/* Optional frontend checks: node --test tests/test_themes.js */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/themes.js'), 'utf8');

function page({url='http://localhost:5000/?job=stable-job', saved=null, storageBlocked=false, loading=false}={}) {
  const listeners = {};
  const elements = {};
  for (const id of ['skinLabel', 'skinToggle', 'skinPicker']) {
    elements[id] = {attributes:{}, setAttribute(k,v){this.attributes[k]=v;}, focus(){this.focused=true;}};
  }
  const buttons = ['fluent', 'win98'].map(skin=>({dataset:{skin},attributes:{},events:{},check:{},setAttribute(k,v){this.attributes[k]=v;},addEventListener(k,fn){this.events[k]=fn;},querySelector(){return this.check;}}));
  elements.skinPicker.querySelectorAll=()=>buttons;
  elements.skinPicker.contains=target=>buttons.includes(target)||target===elements.skinToggle;
  const root={dataset:{}};
  const scrollArea={scrollLeft:240,scrollTop:380};
  const location={href:url,search:new URL(url).search};
  const history={state:{app:'preserved'},writes:0,replaceState(state,_,value){assert.equal(state,this.state);location.href=value.href;location.search=value.search;this.writes++;}};
  const storage=new Map(saved?[['workbenchTheme', saved]]:[]);
  const document={documentElement:root,readyState:loading?'loading':'complete',getElementById:id=>elements[id],querySelectorAll:selector=>selector==='[data-skin]'?buttons:[scrollArea],addEventListener:(event,fn)=>listeners[event]=fn};
  const window={scrollX:0,scrollY:512,scrollTo(position){this.scrollX=position.left;this.scrollY=position.top;}};
  let networkCalls=0;
  const context={document,window,location,history,URL,URLSearchParams,requestAnimationFrame:fn=>fn(),localStorage:{getItem(k){if(storageBlocked)throw new Error('blocked');return storage.get(k)||null;},setItem(k,v){if(storageBlocked)throw new Error('blocked');storage.set(k,v);}},fetch(){networkCalls++;throw new Error('Theme must not make network requests');}};
  vm.runInNewContext(source,context);
  return {root,elements,buttons,scrollArea,location,history,storage,document,listeners,window,networkCalls:()=>networkCalls};
}

test('default and stored preference apply before styles and DOM finish loading',()=>{
  const fresh=page({loading:true});assert.equal(fresh.root.dataset.theme,'fluent');
  const old=page({saved:'win98',loading:true});assert.equal(old.root.dataset.theme,'win98');
  assert.equal(old.history.writes,0);old.listeners.DOMContentLoaded();assert.equal(old.elements.skinLabel.textContent,'Win98');
});

test('explicit URL wins over preference and refreshes remembered skin',()=>{
  const p=page({url:'http://localhost/?theme=fluent&job=abc',saved:'win98'});
  assert.equal(p.root.dataset.theme,'fluent');assert.equal(p.storage.get('workbenchTheme'),'fluent');
});

test('unsupported names and inherited object keys safely fall back',()=>{
  for(const name of ['dark','constructor','toString','__proto__','<script>']){
    const p=page({url:'http://localhost/?theme='+encodeURIComponent(name),saved:'win98'});assert.equal(p.root.dataset.theme,'win98');
  }
  assert.equal(page({saved:'invalid'}).root.dataset.theme,'fluent');
});

test('switch preserves URL parameters, fragment, history state and scroll positions',()=>{
  const p=page({url:'http://localhost/?job=abc&batch=legacy&filter=active#rows'});
  p.buttons[1].events.click();
  const url=new URL(p.location.href);assert.equal(url.searchParams.get('theme'),'win98');
  assert.equal(url.searchParams.get('job'),'abc');assert.equal(url.searchParams.get('batch'),'legacy');assert.equal(url.searchParams.get('filter'),'active');assert.equal(url.hash,'#rows');
  assert.equal(p.history.writes,1);assert.equal(p.root.dataset.theme,'win98');
  assert.equal(p.scrollArea.scrollLeft,240);assert.equal(p.scrollArea.scrollTop,380);assert.equal(p.window.scrollY,512);
  assert.equal(p.buttons[1].attributes['aria-pressed'],'true');assert.equal(p.buttons[0].attributes['aria-pressed'],'false');assert.equal(p.networkCalls(),0);
});

test('storage restrictions do not prevent URL selection or menu switching',()=>{
  const p=page({url:'http://localhost/?theme=win98',storageBlocked:true});assert.equal(p.root.dataset.theme,'win98');
  p.buttons[0].events.click();assert.equal(p.root.dataset.theme,'fluent');assert.equal(new URL(p.location.href).searchParams.get('theme'),'fluent');
});

test('menu can dismiss by Escape without changing the skin',()=>{
  const p=page();p.elements.skinPicker.open=true;let prevented=false;
  p.listeners.keydown({key:'Escape',preventDefault(){prevented=true;}});
  assert.equal(p.elements.skinPicker.open,false);assert.equal(p.elements.skinToggle.focused,true);assert.equal(prevented,true);assert.equal(p.root.dataset.theme,'fluent');
});

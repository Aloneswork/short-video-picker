import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

const html=readFileSync(new URL('../index.html',import.meta.url),'utf8');
const start=html.indexOf('    function bindResultsDelegation() {');
const end=html.indexOf('    function bindSaveBarHeight()',start);
assert(start>=0 && end>start);

const listeners={};
const card={contains:()=>false};
let started=0, stopped=0;
const context={
  $:()=>({addEventListener:(type,handler)=>{listeners[type]=handler;}}),
  hoverCard:null,
  startHoverPreview:()=>{started++;},
  stopHoverPreview:()=>{stopped++;},
};
runInNewContext(`${html.slice(start,end)}\nbindResultsDelegation();`,context);

const target={closest:()=>card};
listeners.pointerover({pointerType:'touch',target,relatedTarget:null});
assert.equal(started,0);
listeners.pointerover({pointerType:'mouse',target,relatedTarget:null});
assert.equal(started,1);
listeners.pointerout({target:{closest:()=>null},relatedTarget:null});
assert.equal(stopped,0);
context.hoverCard=card;
listeners.pointerout({target,relatedTarget:null});
assert.equal(stopped,1);

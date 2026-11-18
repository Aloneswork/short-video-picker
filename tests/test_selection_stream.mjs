import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

const html=readFileSync(new URL('../index.html',import.meta.url),'utf8');
const start=html.indexOf('    function preserveItemState(');
const end=html.indexOf('    async function monitorParse(',start);
assert(start>=0 && end>start);

const state={results:[{source_url:'work-1',items:[
  {kind:'video',suggested_name:'first.mp4',media_url:'low',selected:false,save_state:'saved',quality_label:'低画质'},
  {kind:'image',suggested_name:'cover.jpg',media_url:'cover',selected:false},
]}]};
const incoming=[
  {source_url:'work-1',items:[
    {kind:'video',suggested_name:'first.mp4',media_url:'high',selected:true,preview_url:'new-poster',quality_options:[{url:'high'},{url:'low'}]},
    {kind:'video',suggested_name:'second.mp4',media_url:'second',selected:true},
  ]},
  {source_url:'work-2',items:[{kind:'video',media_url:'third',selected:true}]},
];
const context={state,incoming,merged:null};
runInNewContext(`${html.slice(start,end)}\nmerged=incoming.map((result,index)=>preserveItemState(result,state.results[index]));`,context);
const merged=JSON.parse(JSON.stringify(context.merged));
assert.equal(merged[0].items[0].selected,false);
assert.equal(merged[0].items[0].media_url,'low');
assert.equal(merged[0].items[0].quality_label,'低画质');
assert.equal(merged[0].items[0].save_state,'saved');
assert.equal(merged[0].items[0].preview_url,'new-poster');
assert.equal(merged[0].items[1].selected,true);
assert.equal(merged[1].items[0].selected,true);
assert(html.includes('state.results[group]=preserveItemState(retryResult(previous,status),previous)'));
assert(html.includes('state.results=(status.results||[]).map((result,index)=>preserveItemState(result,state.results[index]))'));

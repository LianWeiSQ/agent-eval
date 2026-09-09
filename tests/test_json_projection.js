const assert=require('node:assert/strict');
const {JsonProjection}=require('../agent_eval/static/json-projection.js');
const source={data:{id:'job-1',name:'中文 😀 \\ " agent_run',config:{repetitions:1},trials:[
  {id:'a',score:0,agent_run:{events:[{text:'escaped \\" } ] , :',nested:[true,false,null,{other:'\n\t'}]}]},grades:[1]},
  {id:'b',score:100,agent_run:null,grades:[]},
  {id:'c',agent_run:'raw \\"string',grades:false},
  {id:'d',agent_run:123,grades:true},
],report:{trials:[{agent_run:'large'}]}},error:null};
const expected=JSON.parse(JSON.stringify(source));
expected.data.trials.forEach(t=>{t.agent_run=null;t.grades=null;});expected.data.report=null;
const serialized=JSON.stringify(source);
for(let size=1;size<90;size++) {
  const parser=new JsonProjection(['agent_run','grades','report']);
  for(let i=0;i<serialized.length;i+=size)parser.feed(serialized.slice(i,i+size));
  assert.deepEqual(parser.finish(),expected,`chunk size ${size}`);
}
const large=new JsonProjection(['agent_run']);
large.feed('{"data":{"trials":[{"id":"a","agent_run":{"events":["');
for(let i=0;i<2000;i++)large.feed('x'.repeat(4096));
large.feed('"]},"outcome":"pass"}]},"error":null}');
assert.ok(large.outputChars<200);
assert.ok(large.parts.join('').length<200);
assert.equal(large.finish().data.trials[0].outcome,'pass');
for(const text of ['{"agent_run":{"unfinished":"abc','{"agent_run":','{"name":"unterminated']) {
  const parser=new JsonProjection(['agent_run']);parser.feed(text);assert.throws(()=>parser.finish());
}
const escapedKey=new JsonProjection(['agent_run']);
escapedKey.feed('{"agent_\\u0072un":[{"x":1}],"safe":2}');
assert.deepEqual(escapedKey.finish(),{agent_run:null,safe:2});
console.log('Streaming JSON projection passed: chunk boundaries, escaped strings/keys, nested values, bounded memory and truncated input.');

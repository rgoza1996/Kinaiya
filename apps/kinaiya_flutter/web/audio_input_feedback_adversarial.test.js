'use strict';
// Supplemental author-owned adversarial tests; actual production bridge in a VM.
// No copied parser, browser devices, project imports or network.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const bridgeFile = path.join(__dirname, 'audio_bridge.js');
const bridgeSource = fs.readFileSync(bridgeFile, 'utf8');
const marker = 'SYNTHETIC-PRIVATE-INPUT-MARKER';
const input = (id, label = 'Synthetic input') => ({kind:'audioinput',deviceId:id,label});
function gate() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise,resolve};
}
function wire(value) {
  assert.equal(typeof value,'string','actual bridge returns JSON strings');
  return JSON.parse(value);
}
function fixture(initialRows) {
  let rows = initialRows, failure = null, held = null;
  const entered = gate();
  const seen = {enumerations:0, gets:[]};
  const mediaDevices = {
    enumerateDevices() {
      seen.enumerations++;
      entered.resolve();
      if (failure !== null) return Promise.reject(failure);
      return held ? held.promise : Promise.resolve(rows);
    },
    getUserMedia(constraints) {
      seen.gets.push(constraints);
      return Promise.reject(new Error('Synthetic capture trap: no hardware'));
    },
    addEventListener() {},
    removeEventListener() {},
  };
  const sandbox = {console,Promise,JSON,Object,Array,Math,Number,Set,Date,
    Uint8Array,Int16Array,Float32Array,ArrayBuffer,DataView,Blob,queueMicrotask,
    setTimeout,clearTimeout,setInterval,clearInterval,
    atob:s=>Buffer.from(s,'base64').toString('binary'),
    btoa:s=>Buffer.from(s,'binary').toString('base64'),
    navigator:{mediaDevices}};
  sandbox.window = {navigator:sandbox.navigator,Blob,
    atob:sandbox.atob,btoa:sandbox.btoa};
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(bridgeSource,sandbox,{filename:bridgeFile});
  return {api:sandbox.window.kinaiyaAudio,seen,entered,
    setRows(value){rows=value;},
    rejectWith(value){failure=value;},
    hold(original){held=original;},
  };
}
function unavailable(result,{reason,explicit=false,failed=false} = {}) {
  assert.equal(result.supported,true);
  assert.equal(result.pending,false);
  assert.equal(result.status,'unavailable');
  assert.equal(result.explicit,explicit);
  assert.equal(result.failed,failed);
  assert.deepEqual(result.devices,[]);
  if (reason === undefined) assert.equal(Object.hasOwn(result,'reason'),false);
  else assert.equal(result.reason,reason);
  assert.equal(JSON.stringify(result).includes(marker),false);
}

test('mixed boundary-sized inventory retains only exposed identifiers without capture',async()=>{
  const rows = Array.from({length:128},(_,index)=>
    input(index === 0 ? '' : String(index).padEnd(4096,'x'),'L'.repeat(4096)));
  const f=fixture(rows);
  try {
    const result=wire(await f.api.inputDevices());
    assert.equal(result.supported,true);
    assert.equal(result.status,'ready');
    assert.equal(result.pending,false);
    assert.equal(result.explicit,false);
    assert.equal(result.failed,false);
    assert.equal(Object.hasOwn(result,'reason'),false);
    assert.deepEqual(result.devices,rows.slice(1).map(row=>({
      id:row.deviceId,label:row.label,selected:false})));
    assert.equal(f.seen.enumerations,1);
    assert.equal(f.seen.gets.length,0);
  } finally {await f.api.dispose();}
});

test('entirely hidden boundary-sized inventory stays explicitly unavailable without capture',async()=>{
  const f=fixture(Array.from({length:128},()=>input('',marker)));
  try {
    unavailable(wire(await f.api.inputDevices()),{reason:'identifiers-unavailable'});
    assert.equal(wire(await f.api.selectInput('mic-safe')).status,'unavailable');
    assert.equal(f.seen.enumerations,2);
    assert.equal(f.seen.gets.length,0);
  } finally {await f.api.dispose();}
});

const laterInvalid = [
  ['nonstring ID', input(42,marker)],
  ['nonstring label', input('mic-other',null)],
  ['duplicate original ID', input('mic-safe',marker)],
  ['ID4097', input('x'.repeat(4097),marker)],
  ['label4097', input('mic-other','L'.repeat(4097))],
];
for (const [name,bad] of laterInvalid) {
  test('redacted ID does not hide later malformed inventory: '+name,async()=>{
    const f=fixture([input('mic-safe'),input('',marker),bad]);
    try {
      unavailable(wire(await f.api.inputDevices()));
      assert.equal(f.seen.enumerations,1);
      assert.equal(f.seen.gets.length,0);
    } finally {await f.api.dispose();}
  });
}
test('raw129 bound is generic even if the first audio input has redacted IDs',async()=>{
  const f=fixture([input('',marker),...Array.from({length:128},(_,i)=>input('mic-'+i))]);
  try {
    unavailable(wire(await f.api.inputDevices()));
    assert.equal(f.seen.enumerations,1);
    assert.equal(f.seen.gets.length,0);
  } finally {await f.api.dispose();}
});

for (const [name,error] of [
  ['Error property',Object.assign(new Error(marker),{reason:'identifiers-unavailable'})],
  ['plain rejection object',{reason:'identifiers-unavailable',message:marker}],
]) {
  test('browser rejection cannot claim the private missing-ID reason: '+name,async()=>{
    const f=fixture([input('mic-safe')]);f.rejectWith(error);
    try {
      unavailable(wire(await f.api.inputDevices()));
      assert.equal(f.seen.enumerations,1,'no discovery retry');
      assert.equal(f.seen.gets.length,0,'no permission warm-up');
    } finally {await f.api.dispose();}
  });
}

for (const [name,rows,reason] of [
  ['mixed inventory without selected ID',[input('mic-b'),input('',marker)],undefined],
  ['entirely hidden inventory',[input('',marker)],'identifiers-unavailable'],
]) test('lost explicit selection refuses default and automatic healing: '+name,async()=>{
  const f=fixture([input('mic-a'),input('mic-b')]);
  try {
    assert.equal(wire(await f.api.selectInput('mic-a')).status,'selected');
    assert.equal(f.seen.gets.length,0);
    f.setRows(rows);
    unavailable(wire(await f.api.inputDevices()),{reason,explicit:true,failed:true});
    assert.equal(await f.api.startArmed(),'unavailable');
    assert.equal(f.seen.gets.length,0,'lost selected input cannot acquire default');
    f.setRows([input('mic-a'),input('mic-b')]);
    unavailable(wire(await f.api.inputDevices()),{explicit:true,failed:true});
    assert.equal(await f.api.startArmed(),'unavailable');
    assert.equal(f.seen.gets.length,0,'a fresh list does not silently heal selection');
    assert.equal(wire(await f.api.selectInput('mic-a')).status,'selected');
    assert.equal(f.seen.gets.length,0,'reselect still does not open a microphone');
    assert.equal(await f.api.startArmed(),'unavailable','synthetic capture trap refuses');
    assert.equal(f.seen.gets.length,1);
    assert.equal(f.seen.gets[0].audio.deviceId.exact,'mic-a');
  } finally {await f.api.dispose();}
});

test('fresh mixed inventory selection and refresh retain the exact exposed input without warming permission',async()=>{
  const f=fixture([input('',marker),input('mic-safe'),input('',marker)]);
  try {
    assert.equal(wire(await f.api.selectInput('')).status,'invalid');
    assert.equal(wire(await f.api.selectInput('mic-safe')).status,'selected');
    const result=wire(await f.api.inputDevices());
    assert.equal(result.status,'ready');
    assert.equal(result.explicit,true);
    assert.equal(result.failed,false);
    assert.deepEqual(result.devices,[{id:'mic-safe',label:'Synthetic input',selected:true}]);
    assert.equal(JSON.stringify(result).includes(marker),false);
    assert.equal(f.seen.enumerations,2);
    assert.equal(f.seen.gets.length,0,'selection and refresh never request capture');
    assert.equal(await f.api.startArmed(),'unavailable','synthetic capture trap refuses');
    assert.equal(f.seen.gets.length,1);
    assert.equal(f.seen.gets[0].audio.deviceId.exact,'mic-safe');
  } finally {await f.api.dispose();}
});

test('held discovery joins the actual original and blocks selection/acquisition without warming permission',async()=>{
  const original=gate(), f=fixture([input('mic-a')]);f.hold(original);
  let first,second,settled=false;
  try {
    first=f.api.inputDevices();
    second=f.api.inputDevices();
    assert.equal(first,second,'both observers retain the same bridge original');
    first.then(()=>{settled=true;},()=>{settled=true;});
    await f.entered.promise;
    assert.equal(f.seen.enumerations,1);
    assert.equal(wire(await f.api.selectInput('mic-a')).status,'busy');
    assert.equal(wire(await f.api.defaultInput()).status,'busy');
    assert.equal(await f.api.startArmed(),'busy');
    await Promise.resolve();await Promise.resolve();
    assert.equal(settled,false,'held original has not been replaced or fabricated');
    assert.equal(f.seen.gets.length,0);
    original.resolve([input('',marker)]);
    unavailable(wire(await first),{reason:'identifiers-unavailable'});
    unavailable(wire(await second),{reason:'identifiers-unavailable'});
    assert.equal(f.seen.enumerations,1);
    assert.equal(f.seen.gets.length,0);
  } finally {
    original.resolve([input('',marker)]);
    await Promise.allSettled([first,second].filter(Boolean));
    await f.api.dispose();
  }
});

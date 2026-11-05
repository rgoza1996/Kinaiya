// Requirement-first public WebAudio/mediaDevices protocol fixtures.
// No real browser devices, inference, or candidate-private helpers.
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function gate() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
function json(value) {
  assert.equal(typeof value, 'string', 'bridge wire remains a JSON string');
  return JSON.parse(value);
}
function clip(value) {
  if (value === '' || value == null) return null;
  const parsed = json(value);
  return parsed.base64 ? parsed : null;
}
function device(id, label = id) { return {kind:'audioinput', deviceId:id, label}; }

function fixture(options = {}) {
  const seen = {gets:[], tracks:[], processors:[], sources:[], recorders:[],
    enumerations:0, now:1000, stopped:0, getEntered:gate(), enumerateEntered:gate()};
  let rows = options.rows || [device('mic-a'), device('mic-b')];
  const listeners = new Set();
  class Track {
    constructor(id) { this.id=id; this.kind='audio'; this.readyState='live'; }
    getSettings() { return {deviceId:this.id}; }
    stop() { this.readyState='ended'; seen.stopped++; }
    addEventListener() {}
    removeEventListener() {}
  }
  class Stream {
    constructor(id) { this.track=new Track(id); seen.tracks.push(this.track); }
    getTracks() { return [this.track]; }
    getAudioTracks() { return [this.track]; }
  }
  class Source {
    constructor() { this.onended=null; this.started=false; this.stops=0; }
    connect() {}
    disconnect() {}
    start() { this.started=true; }
    stop() { this.stops++; queueMicrotask(()=>this.onended && this.onended()); }
  }
  class Context {
    constructor() { this.sampleRate=16000; this.currentTime=1; this.state='running';
      this.destination={}; this.sinkId=''; seen.context=this; }
    resume() { this.state='running'; return Promise.resolve(); }
    close() { this.state='closed'; return Promise.resolve(); }
    setSinkId(id) { this.sinkId=id; return Promise.resolve(); }
    createMediaStreamSource() { return new Source(); }
    createGain() { return {gain:{value:1},connect(){},disconnect(){}}; }
    createAnalyser() { return {fftSize:2048,getFloatTimeDomainData(a){a.fill(0);},
      getByteTimeDomainData(a){a.fill(128);},connect(){},disconnect(){}}; }
    createScriptProcessor(requestedBufferSize) {
      // Zero/omitted means a platform-selected size; this fake chooses4096.
      const bufferSize=requestedBufferSize || 4096;
      assert.ok(Number.isInteger(bufferSize)&&bufferSize>=256&&bufferSize<=16384&&
        (bufferSize&(bufferSize-1))===0,'valid WebAudio processor buffer size');
      const node={requestedBufferSize,bufferSize,onaudioprocess:null,connect(){},disconnect(){}};
      seen.processors.push(node); return node;
    }
    createBuffer(channels, count, rate) {
      const data=new Float32Array(count);
      return {duration:count/rate,getChannelData(){return data;}};
    }
    createBufferSource() { const s=new Source();seen.sources.push(s);return s; }
  }
  if (options.noProcessor) Context.prototype.createScriptProcessor=undefined;
  class Recorder {
    static isTypeSupported() { return true; }
    constructor(stream, opts) { this.stream=stream;this.mimeType=opts && opts.mimeType;
      this.state='inactive';seen.recorders.push(this); }
    start() { this.state='recording'; }
    stop() { this.state='inactive';queueMicrotask(()=>this.onstop && this.onstop()); }
  }
  const mediaDevices = {
    async enumerateDevices() {
      seen.enumerations++;seen.enumerateEntered.resolve();
      return options.enumerationGate ? options.enumerationGate.promise : rows;
    },
    getUserMedia(constraints) {
      seen.gets.push(constraints);seen.getEntered.resolve();
      if (options.permissionGate) return options.permissionGate.promise;
      const exact=constraints.audio && constraints.audio.deviceId &&
        constraints.audio.deviceId.exact;
      const requested=Array.isArray(exact) ? exact[0] : exact;
      return Promise.resolve(new Stream(options.actualId || requested || 'default-mic'));
    },
    addEventListener(type, fn) { if(type==='devicechange') listeners.add(fn); },
    removeEventListener(type, fn) { if(type==='devicechange') listeners.delete(fn); },
  };
  if(options.noEnumeration)delete mediaDevices.enumerateDevices;
  const sandbox={console,Promise,JSON,Object,Array,Math,Number,Uint8Array,Int16Array,
    Float32Array,ArrayBuffer,DataView,Blob,queueMicrotask,
    Date:class extends Date {static now(){return seen.now;}},
    setTimeout:(fn,ms)=>{const t=setTimeout(fn,ms);t.unref();return t;},
    clearTimeout,setInterval:()=>1,clearInterval(){},
    atob:s=>Buffer.from(s,'base64').toString('binary'),
    btoa:s=>Buffer.from(s,'binary').toString('base64'),
    navigator:{mediaDevices},MediaRecorder:Recorder};
  sandbox.window={AudioContext:options.noAudio ? undefined : Context,
    navigator:sandbox.navigator,Blob,atob:sandbox.atob,btoa:sandbox.btoa};
  sandbox.globalThis=sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'audio_bridge.js'),'utf8'),
    sandbox,{filename:'audio_bridge.js'});
  const api=sandbox.window.kinaiyaAudio;
  return {api,seen,Stream,mediaDevices,
    setRows(value){rows=value;},
    async deviceChange(){for(const fn of listeners)fn();
      if(typeof mediaDevices.ondevicechange==='function')mediaDevices.ondevicechange();
      await Promise.resolve();await Promise.resolve();},
    feed(samples) {
      const active=seen.processors.filter(p=>typeof p.onaudioprocess==='function');
      assert.ok(active.length>0,'actual capture graph must be installed');
      for(const p of active) {
        for(let start=0;start<samples.length;start+=p.bufferSize) {
          const block=samples.subarray(start,Math.min(samples.length,start+p.bufferSize));
          p.onaudioprocess({inputBuffer:{
            length:block.length,numberOfChannels:1,sampleRate:16000,
            getChannelData:()=>block}});
        }
      }
    }};
}
async function nativeArm(f) {
  assert.equal(typeof f.api.nativePcmConfigure,'function');
  assert.equal(await f.api.nativePcmConfigure(true),'native-pcm');
  assert.equal(await f.api.startArmed(),'armed');
  await f.api.beginClip();
  return Number(f.api.captureEpoch());
}
function speech(f, epoch) {
  f.seen.now=1000;f.api.vadSample(0.9,1000,epoch);
  f.seen.now=1500;f.api.vadSample(0.9,1500,epoch);
}
function canonicalWav(result, samples) {
  assert.ok(result,'actual completed native WAV required');
  assert.equal(result.mime,'audio/wav');
  const b=Buffer.from(result.base64,'base64');
  assert.equal(b.length,44+2*samples);
  assert.equal(b.toString('ascii',0,4),'RIFF');assert.equal(b.readUInt32LE(4),b.length-8);
  assert.equal(b.toString('ascii',8,12),'WAVE');assert.equal(b.toString('ascii',12,16),'fmt ');
  assert.equal(b.readUInt32LE(16),16);assert.equal(b.readUInt16LE(20),1);
  assert.equal(b.readUInt16LE(22),1);assert.equal(b.readUInt32LE(24),16000);
  assert.equal(b.readUInt32LE(28),32000);assert.equal(b.readUInt16LE(32),2);
  assert.equal(b.readUInt16LE(34),16);assert.equal(b.toString('ascii',36,40),'data');
  assert.equal(b.readUInt32LE(40),samples*2);
  return b;
}

test('native configuration never requests microphone and unsupported graph stays explicit',async()=>{
  const f=fixture();try {
    assert.equal(await f.api.nativePcmConfigure(true),'native-pcm');
    assert.equal(f.seen.gets.length,0);
    assert.equal(await f.api.nativePcmConfigure(false),'legacy');
    assert.equal(f.seen.gets.length,0);
  } finally {await f.api.dispose();}
  const no=fixture({noAudio:true});try {
    assert.equal(await no.api.nativePcmConfigure(true),'unavailable');
    assert.equal(no.seen.gets.length,0);
  } finally {await no.api.dispose();}
});

test('native finish preserves the original PCM as canonical 16k WAV',async()=>{
  const f=fixture();try {
    const epoch=await nativeArm(f);speech(f,epoch);
    f.feed(new Float32Array([-1,0,1]));
    const b=canonicalWav(clip(await f.api.finishClip()),3);
    assert.equal(b.readInt16LE(44),-32768);assert.equal(b.readInt16LE(46),0);
    assert.equal(b.readInt16LE(48),32767);
    const snapshot=Buffer.from(b);
    await f.api.beginClip();f.feed(new Float32Array([0,0]));
    assert.equal(b.equals(snapshot),true,'later capture cannot mutate returned clip');
  } finally {await f.api.dispose();}
});

test('silence alone cannot complete a native utterance',async()=>{
  const f=fixture();try {
    const epoch=await nativeArm(f);f.feed(new Float32Array(16000));
    f.api.vadSample(0,1000,epoch);f.api.vadSample(0,5000,epoch);
    assert.equal(clip(await f.api.finishClip()),null);
  } finally {await f.api.dispose();}
});

for(const overflow of [false,true])test('native thirty-second whole-utterance boundary overflow='+overflow,async()=>{
  const f=fixture();try {
    const epoch=await nativeArm(f);speech(f,epoch);
    const samples=new Float32Array(480000+(overflow?1:0));samples[0]=-1;
    f.feed(samples);
    const result=clip(await f.api.finishClip());
    const events=json(await f.api.pollEvents());
    if(overflow) {
      assert.equal(result,null,'never submit a capped prefix of an oversized utterance');
      assert.ok(events.some(e=>e.type==='native-clip-overflow'&&e.epoch===epoch));
    } else canonicalWav(result,480000);
  } finally {await f.api.dispose();}
});

test('native VAD barge stops original local PCM immediately and fences stale epochs',async()=>{
  const f=fixture();try {
    const epoch=await nativeArm(f);speech(f,epoch);
    await f.api.queuePcm(Buffer.alloc(4800).toString('base64'),24000);
    assert.ok(f.seen.sources.some(s=>s.started));
    assert.equal(f.api.vadSample(0.9,2000,epoch-1),'');
    assert.equal(f.api.playbackActive(),'yes');
    f.api.vadSample(0.9,2100,epoch);
    const event=json(f.api.vadSample(0.9,2600,epoch));
    assert.equal(event.type,'barge');assert.equal(event.epoch,epoch);
    assert.ok(f.seen.sources.some(s=>s.started&&s.stops===1));
    assert.equal(f.api.playbackActive(),'no');
  } finally {await f.api.dispose();}
});

test('input discovery is local bounded enumeration and never opens microphone',async()=>{
  const rows=Array.from({length:128},(_,i)=>device(i===0?'x'.repeat(4096):'mic-'+i,'L'.repeat(4096)));
  const f=fixture({rows});try {
    const result=json(await f.api.inputDevices());
    assert.equal(result.supported,true);assert.equal(result.status,'ready');
    assert.equal(result.devices.length,128);assert.equal(f.seen.gets.length,0);
    assert.equal(result.devices[0].id.length,4096);
    assert.equal(result.devices[0].label.length,4096);
    assert.ok(result.devices.every(row=>typeof row.selected==='boolean'));
  } finally {await f.api.dispose();}
  const unavailable=fixture({noEnumeration:true});try {
    const result=json(await unavailable.api.inputDevices());
    assert.equal(result.supported,false);assert.equal(result.status,'unavailable');
    assert.equal(result.devices.length,0);assert.equal(unavailable.seen.gets.length,0);
  } finally {await unavailable.api.dispose();}
});
for(const rows of [
  Array.from({length:129},(_,i)=>device('mic-'+i)),
  [device('x'.repeat(4097))],
  [device('mic','L'.repeat(4097))],
])test('oversized input discovery refuses whole candidate list '+rows.length+' '+rows[0].deviceId.length+' '+rows[0].label.length,async()=>{
  const f=fixture({rows});try {
    const result=json(await f.api.inputDevices());
    assert.equal(result.status,'unavailable');assert.equal(result.devices.length,0);
    assert.equal(f.seen.gets.length,0);
  } finally {await f.api.dispose();}
});

test('input selection owns fresh enumeration without hidden prior discovery then exact getUserMedia',async()=>{
  const f=fixture();try {
    assert.equal(json(await f.api.selectInput('mic-a')).status,'selected');
    assert.ok(f.seen.enumerations>=1);assert.equal(f.seen.gets.length,0);
    assert.equal(await f.api.startArmed(),'armed');
    const exact=f.seen.gets[0].audio.deviceId.exact;
    if(Array.isArray(exact))assert.deepEqual(Array.from(exact),['mic-a']);
    else assert.equal(exact,'mic-a');
    assert.equal(f.seen.tracks[0].getSettings().deviceId,'mic-a');
    assert.equal(json(await f.api.selectInput('mic-b')).status,'busy');
    assert.equal(json(await f.api.defaultInput()).status,'busy');
    assert.equal(await f.api.nativePcmConfigure(true),'busy');
  } finally {await f.api.dispose();}
});

test('unknown selection leaves prior original input intact before any acquisition',async()=>{
  const f=fixture();try {
    assert.equal(json(await f.api.selectInput('mic-a')).status,'selected');
    for(const bad of [null,42,'','x'.repeat(4097)]) {
      assert.equal(json(await f.api.selectInput(bad)).status,'invalid');
    }
    assert.equal(json(await f.api.selectInput('absent')).status,'invalid');
    assert.equal(await f.api.startArmed(),'armed');
    assert.equal(f.seen.tracks[0].id,'mic-a');
    assert.equal(f.seen.gets.length,1);
  } finally {await f.api.dispose();}
});

test('actual selected-track mismatch never silently falls back to default',async()=>{
  const f=fixture({actualId:'other-actual-device'});try {
    assert.equal(json(await f.api.selectInput('mic-a')).status,'selected');
    assert.notEqual(await f.api.startArmed(),'armed');
    assert.equal(f.seen.gets.length,1);assert.ok(f.seen.stopped>=1);
    assert.notEqual(await f.api.startArmed(),'armed');
    assert.equal(f.seen.gets.length,1,'failed explicit route blocks a replacement default acquisition');
  } finally {await f.api.dispose();}
});

test('removed explicit input stays failed until explicit default recovery',async()=>{
  const f=fixture();try {
    assert.equal(json(await f.api.selectInput('mic-a')).status,'selected');
    f.setRows([device('mic-b')]);await f.deviceChange();
    assert.notEqual(await f.api.startArmed(),'armed');assert.equal(f.seen.gets.length,0);
    const failed=json(await f.api.inputDevices());
    assert.equal(failed.explicit,true);assert.equal(failed.failed,true);
    assert.equal(json(await f.api.defaultInput()).status,'default');
    assert.equal(await f.api.startArmed(),'armed');assert.equal(f.seen.gets.length,1);
  } finally {await f.api.dispose();}
});

test('held acquisition and stop retain original stream and forbid input changes',async()=>{
  const permission=gate();const f=fixture({permissionGate:permission});
  let arming,stopping,joined,stoppedBeforeDisposal;
  try {
    assert.equal(json(await f.api.selectInput('mic-a')).status,'selected');
    arming=f.api.startArmed();
    await f.seen.getEntered.promise;
    assert.equal(f.seen.gets.length,1);
    assert.equal(json(await f.api.selectInput('mic-b')).status,'busy');
    stopping=f.api.stopArmed();
    assert.equal(json(await f.api.defaultInput()).status,'busy');
  } finally {
    if(!stopping)stopping=f.api.stopArmed();
    permission.resolve(new f.Stream('mic-a'));
    joined=await Promise.allSettled([arming,stopping]);
    stoppedBeforeDisposal=f.seen.tracks.length===1&&f.seen.tracks[0].readyState==='ended';
    await f.api.dispose();
  }
  assert.ok(joined.every(result=>result.status==='fulfilled'));
  assert.ok(stoppedBeforeDisposal,'same original late stream stopped before fixture disposal');
  assert.equal(f.seen.gets.length,1);
});

test('pending fresh enumeration has one original and disposal fences its late result',async()=>{
  const enumeration=gate();const f=fixture({enumerationGate:enumeration});
  let selection,arming,disposing,joined;
  try {
    selection=f.api.selectInput('mic-a');
    await f.seen.enumerateEntered.promise;
    assert.equal(f.seen.enumerations,1);
    assert.equal(json(await f.api.selectInput('mic-b')).status,'busy');
    assert.equal(json(await f.api.defaultInput()).status,'busy');
    arming=f.api.startArmed();
    await Promise.resolve();assert.equal(f.seen.gets.length,0);
    disposing=f.api.dispose();
  } finally {
    if(!disposing)disposing=f.api.dispose();
    enumeration.resolve([device('mic-a'),device('mic-b')]);
    joined=await Promise.allSettled([selection,arming,disposing]);
  }
  assert.ok(joined.every(result=>result.status==='fulfilled'));
  assert.notEqual(json(joined[0].value).status,'selected');
  assert.notEqual(joined[1].value,'armed');
  assert.equal(f.seen.gets.length,0);
});

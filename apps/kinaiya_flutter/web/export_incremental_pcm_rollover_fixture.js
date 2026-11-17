// #623: deterministic actual bridge observations, synthetic WebAudio only.
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {fixture}=require('./native_call_input_selection.test.js');

(async()=>{
  const f=fixture();
  assert.equal(f.api.streamPcmConfigure(true),'stream-pcm');
  assert.equal(await f.api.startArmed(),'armed');
  const read=()=>JSON.parse(f.api.streamPcmState());
  const epoch=Number(f.api.captureEpoch());
  f.api.vadSample(0.9,1000,epoch);
  f.feed(new Float32Array([0.5,-0.5]));
  const first=JSON.parse(f.api.streamPcmPeek());
  assert.equal(f.api.streamPcmAck(JSON.stringify({capture_epoch:first.capture_epoch,
    sequence:first.sequence,sample_offset:first.sample_offset,sample_count:first.sample_count})),'confirmed');
  const before=read();
  const original={capture_epoch:before.capture_epoch,next_sequence:before.next_sequence,sample_offset:before.sample_offset};
  f.feed(new Float32Array([0.25,-0.25,1]));
  const retained=read();
  const rotation=JSON.parse(f.api.streamPcmRotate(JSON.stringify(original)));
  const after=read(),packet=JSON.parse(f.api.streamPcmPeek());
  await f.api.stopArmed();
  const stopped=read();
  const output={source:'actual trusted bridge; public synthetic WebAudio; no physical audio or inference',
    before,original,retained,rotation,after,packet,stopped};
  fs.writeFileSync(path.join(__dirname,'../test/fixtures/incremental_pcm_rollover_bridge.json'),JSON.stringify(output,null,2)+'\n');
})().catch(error=>{console.error(error);process.exitCode=1;});

'use strict';
importScripts('./vocal-dsp.js?v=production-1');
self.onmessage=e=>{try{const r=A2ZVocalDSP.process(new Float32Array(e.data.samples),e.data.sampleRate,e.data.options);self.postMessage({ok:true,...r,data:r.data.buffer},[r.data.buffer])}catch(error){self.postMessage({ok:false,error:error.message})}};

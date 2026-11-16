'use strict';
(function(){
const B=window.A2ZBeat;let pattern='A',beatURL=null,beatAudio=new Audio();
function song(){return project.song||(project.song=B.fresh())}
function stop(){beatAudio.pause();beatAudio.removeAttribute('src');beatAudio.load();if(beatURL)URL.revokeObjectURL(beatURL);beatURL=null}
window.stopSongBeat=stop;
function update(change,message){if(busy)return;try{const next=clone(project);next.song=clone(song());change(next.song);B.validate(next.song);stop();commit(next,message)}catch(e){note(e.message);refresh()}}
function grid(){const holder=$('beatGrid');holder.replaceChildren();const p=song().patterns[pattern];for(const row of ['kick','snare','hat','bass']){const line=document.createElement('div');line.style.cssText='display:grid;grid-template-columns:65px repeat(16,44px);gap:5px;margin-bottom:8px;align-items:center;min-width:850px';const label=document.createElement('strong');label.textContent=row==='hat'?'Hi-hat':row[0].toUpperCase()+row.slice(1);line.append(label);for(let i=0;i<16;i++){const el=document.createElement(row==='bass'?'select':'button');el.style.cssText='width:44px;min-height:44px;padding:3px;border-radius:7px;'+(i%4===0?'border-left:3px solid #ffda8a;':'');el.setAttribute('aria-label',row+' step '+(i+1));if(row==='bass'){for(const n of ['','C2','D2','Eb2','F2','G2','Ab2','Bb2','C3'])el.append(new Option(n||'—',n));el.value=p.bass[i];el.onchange=()=>update(s=>s.patterns[pattern].bass[i]=el.value)}else{el.type='button';el.textContent=String(i+1);el.setAttribute('aria-pressed',String(p[row][i]));if(!p[row][i])el.className='secondary';el.onclick=()=>update(s=>s.patterns[pattern][row][i]=!s.patterns[pattern][row][i])}line.append(el)}holder.append(line)}}
function refresh(){let s=song();try{B.validate(s)}catch{note('Song settings could not be read. Existing audio is kept.');project.song=B.fresh();s=project.song}for(const [id,key] of [['beatTempo','tempo'],['beatSwing','swing'],['beatOrder','order'],['beatVolume','volume'],['songLyrics','lyrics']])if(document.activeElement!==$(id))$(id).value=s[key];grid()}
window.refreshSongStudio=refresh;
for(const [id,key] of [['beatTempo','tempo'],['beatSwing','swing'],['beatVolume','volume']])$(id).onchange=()=>update(s=>s[key]=Number($(id).value));$('beatOrder').onchange=()=>update(s=>s.order=$('beatOrder').value.trim().toUpperCase());$('songLyrics').oninput=()=>update(s=>s.lyrics=$('songLyrics').value);
$('beatPattern').onchange=()=>{pattern=$('beatPattern').value;grid()};
$('beatClear').onclick=()=>update(s=>s.patterns[pattern]=B.fresh().patterns.A,'Pattern '+pattern+' cleared.');
$('beatStarter').onclick=()=>update(s=>{const p=B.fresh().patterns.A;for(const i of [0,6,8,14])p.kick[i]=true;for(const i of [4,12])p.snare[i]=true;for(let i=0;i<16;i+=2)p.hat[i]=true;p.bass[0]='C2';p.bass[6]='G2';p.bass[8]='Ab2';p.bass[14]='G2';s.patterns[pattern]=p},'Groove loaded. Tap steps and bass notes to customize it.');
$('beatStop').onclick=stop;
$('beatListen').onclick=async()=>{if(busy)return;try{stop();$('preview').pause();$('audition').pause();const buffer=B.synth(song(),pattern);beatURL=URL.createObjectURL(new Blob([E.wav(buffer)],{type:'audio/wav'}));beatAudio.src=beatURL;beatAudio.loop=true;await beatAudio.play();note('Looping pattern '+pattern+'. Stop before recording.')}catch(e){note('Beat preview: '+e.message)}};
$('beatAdd').onclick=async()=>{if(busy)return;try{stop();const buffer=B.synth(song());if(buffer.duration+playhead>300)throw Error('Beat would extend beyond five minutes. Shorten the order or move the playhead earlier.');const file=new File([E.wav(buffer)],'Beat '+song().tempo+' BPM.wav',{type:'audio/wav'});await addSources([file]);}catch(e){note(e.message)}};
window.addEventListener('pagehide',stop);initialAudioLoad.then(refresh);
})();

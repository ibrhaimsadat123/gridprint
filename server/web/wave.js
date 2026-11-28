// GridPrint page transitions: leaving a page, a sine wave rises and floods the screen while the camera pans
// along it; it keeps oscillating while the next page loads, then drains away to reveal it.
// Loaded synchronously in <head> so the arriving page is covered before its first paint.
(()=>{
'use strict';
if(window.__gpWave) return; window.__gpWave=true;
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
const KEY='gp_wave', DEEP='#0A1120', AMBER='#F2A23A';
let cv=null, ctx=null, W=0, H=0, DPR=1, raf=0;
const st={level:1.2, amp:0, phase:0, speed:6, k:1, line:1, mode:'idle', t0:0};
function mount(){
  if(cv) return; cv=document.createElement('canvas');
  cv.setAttribute('aria-hidden','true');
  cv.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;z-index:2147483000;pointer-events:none;display:block';
  (document.body||document.documentElement).appendChild(cv); ctx=cv.getContext('2d'); size();
  addEventListener('resize',size);
}
function size(){ if(!cv) return; DPR=Math.min(2,devicePixelRatio||1); W=innerWidth; H=innerHeight; cv.width=W*DPR; cv.height=H*DPR }
function y(x,base,amp,ph,k){ // a grid-frequency-like wave: fundamental plus two quieter harmonics
  const u=x/W*Math.PI*2*k;
  return base+amp*(Math.sin(u+ph)+0.32*Math.sin(2.1*u-ph*1.3)+0.16*Math.sin(3.7*u+ph*0.7));
}
function draw(){
  const c=ctx; c.setTransform(DPR,0,0,DPR,0,0); c.clearRect(0,0,W,H);
  const base=H*st.level, amp=H*st.amp;
  // trailing ghosts: the camera has just passed them
  for(let g=3;g>=1;g--){
    c.beginPath(); for(let x=-10;x<=W+10;x+=6){const yy=y(x,base-g*amp*0.45,amp*(1-g*0.18),st.phase-g*0.6,st.k); x<0?c.moveTo(x,yy):c.lineTo(x,yy)}
    c.strokeStyle=`rgba(242,162,58,${0.10/g})`; c.lineWidth=1.5; c.stroke();
  }
  // the flood below the crest
  c.beginPath(); c.moveTo(-10,H+10);
  for(let x=-10;x<=W+10;x+=5) c.lineTo(x,y(x,base,amp,st.phase,st.k));
  c.lineTo(W+10,H+10); c.closePath();
  const g=c.createLinearGradient(0,Math.max(0,base-amp),0,H); g.addColorStop(0,'#13213D'); g.addColorStop(0.25,DEEP); g.addColorStop(1,DEEP);
  c.fillStyle=g; c.fill();
  // the crest itself, glowing
  c.beginPath(); for(let x=-10;x<=W+10;x+=4){const yy=y(x,base,amp,st.phase,st.k); x<0?c.moveTo(x,yy):c.lineTo(x,yy)}
  c.strokeStyle='rgba(242,162,58,.18)'; c.lineWidth=14; c.stroke(); c.strokeStyle='rgba(242,162,58,.35)'; c.lineWidth=6; c.stroke(); c.strokeStyle=AMBER; c.lineWidth=2.4; c.stroke();
  // once the screen is covered, an oscilloscope trace keeps humming in the middle until the next page is ready
  if(st.mode==='hold'||st.mode==='in'){
    const a=Math.max(0,1-(st.level<0.05?0:st.level*3));
    if(a>0){c.globalAlpha=a; c.beginPath(); for(let x=0;x<=W;x+=4){const yy=y(x,H*0.5,H*0.045,st.phase*1.6,1.4); x?c.lineTo(x,yy):c.moveTo(x,yy)}
      c.strokeStyle='rgba(242,162,58,.25)'; c.lineWidth=10; c.stroke(); c.strokeStyle=AMBER; c.lineWidth=2; c.stroke();
      c.font='600 13px "IBM Plex Sans",system-ui'; c.fillStyle='rgba(234,240,248,.55)'; c.textAlign='center'; c.fillText('tuning in…',W/2,H*0.5+H*0.045+38); c.globalAlpha=1}
  }
}
function loop(now){
  const dt=Math.min(0.05,(now-(loop.last||now))/1000); loop.last=now;
  st.phase+=dt*st.speed; tick(now); draw();
  if(st.mode!=='idle') raf=requestAnimationFrame(loop); else {cv&&cv.remove(); cv=null}
}
const ease=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
function tick(now){
  const t=(now-st.t0)/1000;
  if(st.mode==='out'){ // rise: the crest climbs from below the screen past the top, wavelength tightening as the camera pushes in
    const p=Math.min(1,t/0.85), e=ease(p);
    st.level=1.15-1.45*e; st.amp=0.02+0.10*Math.sin(Math.PI*Math.min(1,p*1.1)); st.k=0.8+1.4*e; st.speed=5+9*e;
    if(p>=1){st.mode='hold'; st.t0=now}
  } else if(st.mode==='hold'){ st.level=-0.3; st.amp=0.02; st.speed=7 }
  else if(st.mode==='in'){ // drain: the crest falls back through the page and out the bottom
    const p=Math.min(1,t/0.95), e=ease(p);
    st.level=-0.3+1.5*e; st.amp=0.02+0.09*Math.sin(Math.PI*p); st.k=2.2-1.4*e; st.speed=12-7*e;
    if(p>=1){st.mode='idle'}
  }
}
function start(mode){ mount(); st.mode=mode; st.t0=performance.now(); cancelAnimationFrame(raf); loop.last=0; raf=requestAnimationFrame(loop) }

// ---- arriving: cover first, drain once the page has painted
let arriving=false;
try{const v=+sessionStorage.getItem(KEY)||0; sessionStorage.removeItem(KEY); arriving=!RM&&Date.now()-v<8000}catch(e){}
if(arriving){
  st.mode='hold'; st.level=-0.3; mount(); draw();
  const go=()=>setTimeout(()=>{start('hold'); setTimeout(()=>start('in'),180)},40);
  if(document.readyState==='complete') go(); else addEventListener('load',go,{once:true});
  setTimeout(()=>{if(st.mode==='hold') start('in')},2500);   // never trap the page behind the wave
}

// ---- leaving: links and forms marked data-go
function leave(fn){
  if(RM){fn();return}
  try{sessionStorage.setItem(KEY,String(Date.now()))}catch(e){}
  start('out'); setTimeout(fn,620);   // start loading the next page while the wave is still rising
}
document.addEventListener('click',e=>{
  const a=e.target.closest&&e.target.closest('a[data-go]'); if(!a||e.defaultPrevented) return;
  if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||a.target==='_blank') return;
  const u=new URL(a.href,location.href); if(u.origin!==location.origin) return;
  if(u.pathname===location.pathname&&u.hash) return;
  e.preventDefault(); leave(()=>{location.href=a.href});
},true);
document.addEventListener('submit',e=>{
  const f=e.target; if(!f.matches||!f.matches('form[data-go]')||e.defaultPrevented) return;
  if(f.checkValidity&&!f.checkValidity()) return;
  e.preventDefault(); leave(()=>HTMLFormElement.prototype.submit.call(f));
},true);
addEventListener('pageshow',e=>{if(e.persisted){st.mode='idle'; if(cv){cv.remove(); cv=null}}});
})();

// GridPrint team page: a field of grid waves in perspective behind two 3D profile cards that tilt toward the
// pointer and flip over to show how to get in touch.
(()=>{
'use strict';
const G=window.gsap, RM=matchMedia('(prefers-reduced-motion: reduce)').matches, anim=!!G&&!RM;
// ---------------------------------------------------------------- background: wave strings receding into the distance
const cv=document.getElementById('bg'), c=cv.getContext('2d'); let W=0,H=0,DPR=1, mx=0.5, my=0.5;
function size(){DPR=Math.min(2,devicePixelRatio||1); W=innerWidth; H=innerHeight; cv.width=W*DPR; cv.height=H*DPR}
addEventListener('resize',size); size();
addEventListener('pointermove',e=>{mx=e.clientX/W; my=e.clientY/H},{passive:true});
let sx=0.5, sy=0.5;
function frame(now){
  const t=now/1000; sx+=(mx-sx)*0.04; sy+=(my-sy)*0.04;
  c.setTransform(DPR,0,0,DPR,0,0); c.clearRect(0,0,W,H);
  const g=c.createRadialGradient(W*0.7,H*0.25,0,W*0.7,H*0.25,Math.max(W,H)); g.addColorStop(0,'#13213D'); g.addColorStop(1,'#0A1120'); c.fillStyle=g; c.fillRect(0,0,W,H);
  const N=26, hor=H*(0.42+(sy-0.5)*0.06), vx=W*(0.5+(sx-0.5)*0.12);
  for(let i=N;i>=1;i--){
    const z=i/N, p=1/(0.35+z*2.2);                  // perspective: far strings are small and close to the horizon
    const yb=hor+(H*0.9)*p*0.55, amp=H*0.05*p, k=0.9+z*2;
    c.beginPath();
    for(let x=-20;x<=W+20;x+=8){const u=(x-vx)/W*Math.PI*2*k; const yy=yb+amp*(Math.sin(u+(RM?0:t*0.8)+i*0.7)+0.3*Math.sin(2.3*u-(RM?0:t*1.1)+i)); x<0?c.moveTo(x,yy):c.lineTo(x,yy)}
    const front=i===1; c.strokeStyle=front?'rgba(242,162,58,.85)':`rgba(124,141,176,${0.06+0.28*(1-z)})`; c.lineWidth=front?2.2:1;
    c.stroke(); if(front){c.strokeStyle='rgba(242,162,58,.2)'; c.lineWidth=9; c.stroke()}
  }
  if(!RM) requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---------------------------------------------------------------- cards: tilt, glare, flip
document.querySelectorAll('.card').forEach(card=>{
  const flip=card.querySelector('.flip');
  const rx=anim?G.quickTo(card,'rotationX',{duration:.6,ease:'power3.out'}):null, ry=anim?G.quickTo(card,'rotationY',{duration:.6,ease:'power3.out'}):null;
  card.addEventListener('pointermove',e=>{ if(!anim) return; const r=card.getBoundingClientRect(), px=(e.clientX-r.left)/r.width, py=(e.clientY-r.top)/r.height;
    rx((0.5-py)*14); ry((px-0.5)*16);
    card.querySelectorAll('.glare').forEach(gl=>{gl.style.setProperty('--gx',px*100+'%'); gl.style.setProperty('--gy',py*100+'%')})});
  card.addEventListener('pointerleave',()=>{if(anim){rx(0); ry(0)}});
  const toggle=open=>{card.classList.toggle('open',open); card.setAttribute('aria-expanded',String(open));
    if(anim) G.fromTo(card,{z:0},{z:60,duration:.45,yoyo:true,repeat:1,ease:'power2.out'})};
  card.addEventListener('click',e=>{ if(e.target.closest('a.mail')) return; if(e.target.closest('.backbtn')){toggle(false); return} toggle(!card.classList.contains('open')) });
  card.addEventListener('keydown',e=>{ if(e.target!==card) return; if(e.key==='Enter'||e.key===' '){e.preventDefault(); toggle(!card.classList.contains('open'))} });
});

// ---------------------------------------------------------------- arrival
if(anim){
  const parts=window.SplitText?new SplitText('h1',{type:'words,chars'}).chars:['h1'];
  G.timeline({defaults:{ease:'expo.out'}})
   .from(parts,{yPercent:110,rotateX:-80,opacity:0,duration:1.1,stagger:.02},0.35)
   .from('.lede',{y:24,opacity:0,duration:.9},0.7)
   .from('.card',{y:120,rotationX:-35,rotationY:(i)=>i?18:-18,opacity:0,duration:1.6,stagger:.15,transformOrigin:'50% 100%'},0.75)
   .from('.portrait',{scale:.6,opacity:0,duration:1.2,stagger:.15,ease:'back.out(1.6)'},1.05)
   .from('.together',{y:30,opacity:0,duration:.9},1.3);
}
})();

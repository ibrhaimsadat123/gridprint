// GridPrint explorer: motion and instruments layered on top of app.js. app.js keeps doing the data work and
// writes plain text into the page; this file watches those elements and turns each change into motion.
(()=>{
'use strict';
const G=window.gsap, $=id=>document.getElementById(id);
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
const anim=!!G&&!RM;
const C={sodium:'#F2A23A',violet:'#A493FF',ok:'#3DD68C',bad:'#F2555A',warn:'#FF8F6B',mut:'#8494B2'};
const watch=(el,fn,opts)=>{if(!el) return; new MutationObserver(fn).observe(el,opts||{childList:true,characterData:true,subtree:true}); fn()};

// ---------------------------------------------------------------- odometer
const odo=$('odo'); let odoText='';
function buildOdo(txt){
  odo.innerHTML='';
  const m=txt.match(/^([\d.]+)\s*(Hz)?$/);
  if(!m){const s=document.createElement('span'); s.textContent=txt==='–'?'—':txt; s.style.fontSize=txt.length>4?'.6em':''; odo.appendChild(s); return}
  for(const ch of m[1]){
    if(/\d/.test(ch)){const col=document.createElement('span'); col.className='col'; const strip=document.createElement('span'); strip.className='strip';
      for(let d=0;d<10;d++){const s=document.createElement('span'); s.textContent=d; strip.appendChild(s)} col.appendChild(strip); col.dataset.d='0'; odo.appendChild(col)}
    else{const s=document.createElement('span'); s.textContent=ch; odo.appendChild(s)}
  }
  if(m[2]){const u=document.createElement('span'); u.className='u'; u.textContent='Hz'; odo.appendChild(u)}
}
function setOdo(txt){
  const shape=t=>t.replace(/\d/g,'0');
  if(shape(txt)!==shape(odoText)||!odo.firstChild) buildOdo(txt);
  odoText=txt;
  const digits=(txt.match(/[\d.]+/)||[''])[0].replace('.','');
  odo.querySelectorAll('.col').forEach((col,i)=>{const d=+digits[i]; if(col.dataset.d===String(d)&&col._set) return;
    const strip=col.firstChild, y=-d*10+'%';
    if(G){ if(anim&&col._set) G.to(strip,{yPercent:-d*10,duration:.55+i*.05,ease:'power3.out',overwrite:true}); else G.set(strip,{yPercent:-d*10}) }
    else strip.style.transform=`translateY(${y})`;
    col.dataset.d=d; col._set=true});
}

// ---------------------------------------------------------------- gauge (49.5 to 50.5 Hz over 220 degrees)
const NS='http://www.w3.org/2000/svg', CX=150, CY=150, RAD=112, SWEEP=110;
const ang=f=>Math.max(-1,Math.min(1,(f-50)/0.5))*SWEEP;
function polar(a,r){const t=(a-90)*Math.PI/180; return [CX+r*Math.cos(t),CY+r*Math.sin(t)]}
function arc(a0,a1,r){const [x0,y0]=polar(a0,r),[x1,y1]=polar(a1,r); return `M${x0} ${y0} A${r} ${r} 0 ${a1-a0>180?1:0} 1 ${x1} ${y1}`}
function el(tag,attrs,parent){const e=document.createElementNS(NS,tag); for(const k in attrs) e.setAttribute(k,attrs[k]); parent.appendChild(e); return e}
(function buildGauge(){
  const b=$('gBands'), t=$('gTicks');
  el('path',{d:arc(-SWEEP,SWEEP,RAD),stroke:'#1B2945','stroke-width':16,fill:'none','stroke-linecap':'round'},b);
  el('path',{d:arc(ang(49.8),ang(50.2),RAD),stroke:'rgba(242,162,58,.22)','stroke-width':16,fill:'none'},b);
  el('path',{d:arc(ang(49.95),ang(50.05),RAD),stroke:'rgba(242,162,58,.5)','stroke-width':16,fill:'none'},b);
  window.__trail=el('path',{d:arc(0,0.01,RAD-22),stroke:C.sodium,'stroke-width':3,fill:'none','stroke-linecap':'round',opacity:.55},b);
  for(let f=49.5;f<=50.5001;f+=0.05){const a=ang(f), major=Math.abs((f*10)-Math.round(f*10))<1e-6, [x0,y0]=polar(a,RAD+11), [x1,y1]=polar(a,RAD+(major?22:17));
    el('line',{x1:x0,y1:y0,x2:x1,y2:y1,class:'tick'+(major?' major':'')},t)}
})();
const needle={a:-SWEEP}; let needleColor=C.sodium;
function setGauge(f,color){
  const a=f==null?-SWEEP:ang(f);
  const apply=()=>{$('needle').setAttribute('transform',`rotate(${needle.a} ${CX} ${CY})`);
    window.__trail.setAttribute('d',needle.a>=0?arc(0,Math.max(needle.a,0.01),RAD-22):arc(needle.a,0,RAD-22))};
  if(anim) G.to(needle,{a,duration:1.4,ease:'elastic.out(1,0.55)',onUpdate:apply}); else {needle.a=a; apply()}
  if(color!==needleColor){needleColor=color; $('needle').querySelectorAll('line,circle:first-of-type').forEach(n=>{n.setAttribute(n.tagName==='line'?'stroke':'fill',color)}); window.__trail.setAttribute('stroke',color)}
  const dev=$('dev');
  if(f==null){dev.innerHTML='&nbsp;';return}
  const mhz=Math.round((f-50)*1000);
  dev.innerHTML=mhz===0?'<b>exactly on 50 Hz</b>':`<b>${Math.abs(mhz)} mHz</b> ${mhz>0?'above':'below'} 50 Hz · ${mhz>0?'more power than demand':'more demand than power'}`;
}

// ---------------------------------------------------------------- live readout -> odometer, gauge, status pill
function syncNow(){
  const txt=$('cNow').textContent.trim(), st=$('cStatus').textContent.trim();
  setOdo(txt);
  const f=parseFloat(txt), live=!isNaN(f)&&/Hz/.test(txt);
  const color=st==='ON BACKUP'?C.violet:st==='OUTAGE'?C.bad:st==='GLITCH'?C.warn:live?C.sodium:C.mut;
  setGauge(live&&st!=='OUTAGE'?f:null,color);
  const pill=$('pill'), dot=pill.querySelector('i'), label=st&&st!=='–'?st.charAt(0)+st.slice(1).toLowerCase():'connecting';
  if($('pillTxt').textContent!==label){$('pillTxt').textContent=label; if(anim) G.fromTo(pill,{scale:.92},{scale:1,duration:.5,ease:'back.out(3)'})}
  dot.style.background=color; dot.style.color=color;
  if(anim&&!dot._pulse){dot._pulse=G.to(dot,{boxShadow:'0 0 0 7px rgba(0,0,0,0)',repeat:-1,duration:1.6,ease:'power2.out',startAt:{boxShadow:'0 0 0 0 currentColor'}})}
}
watch($('cNow'),syncNow); watch($('cStatus'),syncNow);

// ---------------------------------------------------------------- the probable cause, shown in the status panel too
watch($('orbPop'),()=>{const c=$('orbPop').querySelector('.cause'), w=$('why'); if(!c||!w) return; const t=c.textContent.trim(); if(w.dataset.t===t) return; w.dataset.t=t;
  w.innerHTML='<b>Probable cause</b>'; w.appendChild(document.createTextNode(t)); if(anim) G.fromTo(w,{opacity:0,y:8},{opacity:1,y:0,duration:.6,ease:'power3.out'})});

// ---------------------------------------------------------------- preset thumb that slides between buttons (GSAP Flip)
const seg=$('presets'), thumb=$('thumb');
function moveThumb(first){
  const on=seg.querySelector('button.on'); if(!on){thumb.style.opacity=0;return}
  thumb.style.opacity=1; const box={x:on.offsetLeft-4,w:on.offsetWidth};
  if(anim&&!first) G.to(thumb,{x:box.x,width:box.w,duration:.55,ease:'expo.out'}); else {thumb.style.width=box.w+'px'; thumb.style.transform=`translateX(${box.x}px)`; if(G) G.set(thumb,{x:box.x,width:box.w})}
}
if(seg){seg.classList.add('fx'); moveThumb(true); new MutationObserver(()=>moveThumb(false)).observe(seg,{attributes:true,subtree:true,attributeFilter:['class']}); addEventListener('resize',()=>moveThumb(true))}

// ---------------------------------------------------------------- chart: every new range eases in
let lastRange='';
watch($('cRange'),()=>{const r=$('cRange').textContent; if(r===lastRange) return; lastRange=r;
  if(!anim) return; const c=$('chart').querySelectorAll('canvas');
  G.fromTo(c,{opacity:.25,filter:'blur(4px) saturate(.4)'},{opacity:1,filter:'blur(0px) saturate(1)',duration:.7,ease:'power2.out',overwrite:true});
  G.fromTo('#cRange',{yPercent:30,opacity:0},{yPercent:0,opacity:1,duration:.5,ease:'power3.out'})});
watch($('chart'),m=>{if(!anim) return; const u=$('chart').querySelector('.uplot'); if(u&&!u._in){u._in=true; G.from(u,{clipPath:'inset(0 100% 0 0)',duration:1.1,ease:'expo.inOut',clearProps:'clipPath'})}},{childList:true});

// ---------------------------------------------------------------- tables: rows arrive in a quick cascade
function cascade(tb){let n=0; watch(tb,()=>{const rows=[...tb.children]; if(!anim||rows.length===n||rows.length>60){n=rows.length;return} n=rows.length;
  G.fromTo(rows,{opacity:0,y:10},{opacity:1,y:0,duration:.45,stagger:.025,ease:'power2.out',overwrite:true})},{childList:true})}
cascade($('events')); cascade($('stats'));
watch($('cMean'),()=>{if(anim) G.fromTo(['#cMean','#cOut'],{opacity:0,x:-8},{opacity:1,x:0,duration:.5,stagger:.06})},{childList:true,characterData:true,subtree:true});

// ---------------------------------------------------------------- side panel and report drawer
const det=$('details');
let detWas=det.style.display!=='none';
new MutationObserver(()=>{const open=det.style.display!=='none'; if(open===detWas) return; detWas=open;
  dispatchEvent(new Event('resize'));
  if(open&&anim) G.fromTo(det,{opacity:0,x:30},{opacity:1,x:0,duration:.6,ease:'expo.out'});
}).observe(det,{attributes:true,attributeFilter:['style']});
const card=$('reportCard'), scrim=$('scrim');
let closing=false, repOpen=false;
new MutationObserver(()=>{if(closing) return; const open=card.style.display==='block'; if(open===repOpen) return; repOpen=open; scrim.style.display=open?'block':'none';
  if(open&&anim){G.fromTo(card,{xPercent:100},{xPercent:0,duration:.75,ease:'expo.out'}); G.fromTo(scrim,{opacity:0},{opacity:1,duration:.4})}
}).observe(card,{attributes:true,attributeFilter:['style']});
watch($('report'),()=>{if(!anim) return; const kids=[...$('report').children]; if(kids.length>1) G.fromTo(kids,{opacity:0,y:14},{opacity:1,y:0,duration:.6,stagger:.04,ease:'power3.out',overwrite:true})},{childList:true});
function closeReport(){if(card.style.display!=='block') return;
  const done=()=>{closing=true; card.style.display='none'; scrim.style.display='none'; repOpen=false; closing=false};
  if(anim){G.to(card,{xPercent:100,duration:.5,ease:'expo.in',onComplete:done}); G.to(scrim,{opacity:0,duration:.4})} else done()}
$('repClose').addEventListener('click',closeReport); scrim.addEventListener('click',closeReport);
addEventListener('keydown',e=>{if(e.key==='Escape') closeReport()});

// ---------------------------------------------------------------- admin dialog
const dlg=$('adminDlg');
new MutationObserver(()=>{if(dlg.open&&anim) G.fromTo(dlg,{opacity:0,y:20,scale:.97},{opacity:1,y:0,scale:1,duration:.5,ease:'expo.out'})}).observe(dlg,{attributes:true,attributeFilter:['open']});

// ---------------------------------------------------------------- smooth scroll (the chart, report and dialog keep their own wheel)
if(window.Lenis&&anim){const lenis=new Lenis({lerp:0.1}); G.ticker.add(t=>lenis.raf(t*1000)); G.ticker.lagSmoothing(0)}

// ---------------------------------------------------------------- arrival and departure
if(anim){
  G.timeline({defaults:{ease:'expo.out'}})
    .from('.appbar',{y:-40,opacity:0,duration:.8})
    .from('.deck .panel',{y:40,opacity:0,duration:1,stagger:.09},.1)
    .from('.gauge',{rotate:-25,opacity:0,transformOrigin:'50% 86%',duration:1.4},.3)
    .from('.controls > *',{y:16,opacity:0,duration:.7,stagger:.05},.35)
    .from('#chart',{y:30,opacity:0,duration:1},.45)
    .from(['#slider','.slLbl'],{opacity:0,duration:.6},.8)
    .from('.lower .panel',{y:40,opacity:0,duration:.9,stagger:.1},.6);
}
})();

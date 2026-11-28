// GridPrint home. WebGL landscape of the live grid (hand-written, ~5 KB instead of a 3D library),
// GSAP ScrollTrigger drives the camera, Lenis smooths the scroll, SplitText animates the type.
(()=>{
'use strict';
const PREFIX=document.querySelector('script[src*="/static/home.js"]').getAttribute('src').replace(/\/static\/home\.js.*$/,'');
const PROOF=__PROOF__;
const C={deep:'#0A1120',sodium:'#F2A23A',cyan:'#6FD6FF',violet:'#A493FF',mut:'#8494B2',ink:'#EAF0F8'};
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
const G=window.gsap, ST=window.ScrollTrigger, SPLIT=window.SplitText;
const $=id=>document.getElementById(id);
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x)), lerp=(a,b,t)=>a+(b-a)*t;
const ease=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
const seg=(p,a,b)=>clamp((p-a)/(b-a),0,1);
const PKT=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Karachi',hour:'numeric',minute:'2-digit'});
const PKTs=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Karachi',hour:'numeric',minute:'2-digit',second:'2-digit'});
const hex=h=>[parseInt(h.slice(1,3),16)/255,parseInt(h.slice(3,5),16)/255,parseInt(h.slice(5,7),16)/255];

// ---------------------------------------------------------------- live data
let pulse=null, rows=null, ymid=50, yspan=0.3, meshDirty=true;
async function loadPulse(){
  try{const r=await fetch(PREFIX+'/api/pulse',{cache:'no-store'}); if(!r.ok) throw 0; pulse=await r.json(); buildRows(); showLive()}
  catch(e){ $('ls').textContent='live data unavailable'; if(!rows) fakeRows() }
}
function showLive(){
  const p=pulse, live=$('live'), now=Date.now()/1000;
  if(!p||p.f_now==null||!p.last_seen||now-p.last_seen>120){$('lv').textContent='offline';$('ls').textContent=p&&p.last_seen?'last reading '+PKT.format(p.last_seen*1000):'station offline';return}
  const bk=(p.backup||[]).find(b=>b[3]&&b[1]>=p.last_seen-40);
  live.classList.toggle('backup',!!bk);
  const txt=bk?'on backup':p.f_now.toFixed(3)+' Hz';
  if($('lv').textContent!==txt){$('lv').textContent=txt; if(G&&!RM) G.fromTo('#lv',{yPercent:40,opacity:0},{yPercent:0,opacity:1,duration:.5,ease:'power3.out'})}
  $('ls').textContent=bk?'mains off since '+PKT.format(bk[0]*1000):(p.trend>0.004?'rising':p.trend<-0.004?'falling':'holding')+', updated '+Math.max(1,Math.round(now-p.last_seen))+' s ago';
}
function buildRows(){
  const p=pulse; if(!p||!p.f||!p.f.length) return fakeRows();
  const out=[], bk=p.backup||[];
  for(let i=0;i<p.f.length;i+=60){const r=[];
    for(let k=0;k<60;k++){const j=i+k, v=j<p.f.length&&p.f[j]!=null?50+p.f[j]/1e4:null, t=p.t0+j*p.step; r.push({v,b:bk.some(s=>t>=s[0]&&t<=s[1])})}
    out.push(r)}
  const vals=p.f.filter(x=>x!=null).map(x=>50+x/1e4);
  if(vals.length){const lo=Math.min(...vals),hi=Math.max(...vals); ymid=(lo+hi)/2; yspan=Math.max(0.12,(hi-lo)/2)}
  rows=out.slice(-36); meshDirty=true;
}
function fakeRows(){
  rows=[]; for(let r=0;r<36;r++){const row=[];for(let k=0;k<60;k++)row.push({v:50+0.12*Math.sin((r*60+k)/37)+0.05*Math.sin((r*60+k)/9),b:false});rows.push(row)}
  ymid=50; yspan=0.2; meshDirty=true; $('axis').textContent='Live data is unavailable right now, so this landscape is a placeholder shape.';
}

// ---------------------------------------------------------------- camera (shared by WebGL and the 2D overlay)
const glc=$('terrain'), fx=$('fx'), cx=fx.getContext('2d');
let W=0,H=0,DPR=1;
function camAt(p){
  const down=ease(seg(p,0.12,0.6)), mob=W<760;
  const yaw=RM?0:Math.sin(performance.now()/9000)*0.22;
  return {p,down,mob,pitch:lerp(0.62,0,down),yw:lerp(yaw*0.6-0.32,0,down),dist:lerp(3.9,3.1,down),
    f:mob?W*0.95:Math.min(W*0.62,H*1.05),ox:W*(mob?0.5:lerp(0.68,0.5,down)),oy:H*(mob?lerp(0.3,0.36,down):lerp(0.5,0.47,down)),
    focus:ease(seg(p,0.36,0.56)),flat:ease(seg(p,0.58,0.68))};
}
const xOf=k=>lerp(-1.55,1.55,k/59);
const zOf=(r,R)=>lerp(1.7,-0.9,R>1?r/(R-1):1);
function projector(c){const cy=Math.cos(c.yw),sy=Math.sin(c.yw),cp=Math.cos(c.pitch),sp=Math.sin(c.pitch);
  return (x,y,z)=>{const X=x*cy-z*sy, Z0=x*sy+z*cy, Y=y*cp+Z0*sp, Z=Z0*cp-y*sp, d=Z+c.dist; return [c.ox+X*c.f/d, c.oy-Y*c.f/d, d]}}

// ---------------------------------------------------------------- WebGL terrain
let gl=null, prog=null, buf={}, nFill=0, parts=null;
const VS=`attribute vec3 aPos;attribute vec3 aInfo;uniform vec4 uCam;uniform vec4 uView;uniform vec2 uRes;
varying vec3 vInfo;varying float vDepth;
void main(){float cy=cos(uCam.x),sy=sin(uCam.x),cp=cos(uCam.y),sp=sin(uCam.y);
 float X=aPos.x*cy-aPos.z*sy, Z0=aPos.x*sy+aPos.z*cy, Y=aPos.y*cp+Z0*sp, Z=Z0*cp-aPos.y*sp, d=Z+uCam.z;
 vec2 s=vec2(uView.x+X*uCam.w/d, uView.y-Y*uCam.w/d);
 gl_Position=vec4(s.x/uRes.x*2.0-1.0, 1.0-s.y/uRes.y*2.0, clamp((d-0.5)/8.0,0.0,1.0)*2.0-1.0, 1.0);
 vInfo=aInfo; vDepth=d; gl_PointSize=1.0;}`;
const FS=`precision mediump float;varying vec3 vInfo;varying float vDepth;uniform vec4 uMode;uniform vec3 uA;uniform vec3 uB;uniform vec3 uV;
void main(){float age=vInfo.x, top=vInfo.y, bk=vInfo.z;
 float fade=mix(1.0,0.07,uMode.y)*(1.0-uMode.z);
 if(uMode.x<0.5){
   vec3 deep=vec3(0.039,0.067,0.125); vec3 c=mix(deep,vec3(0.10,0.15,0.26),pow(top,3.0)*(0.35+0.65*age));
   gl_FragColor=vec4(c,0.94*fade);
 } else {
   vec3 c=mix(uA,uB,pow(age,1.6)); c=mix(c,uV,bk);
   float fog=clamp(1.15-(vDepth-2.2)*0.28,0.25,1.0);
   gl_FragColor=vec4(c*fog,fade);
 }}`;
const PVS=`attribute vec3 aPos;uniform vec4 uCam;uniform vec4 uView;uniform vec2 uRes;uniform float uT;varying float vA;
void main(){vec3 p=aPos; p.y=mod(p.y+uT*0.018*(0.5+fract(p.x*7.13)),1.6)-0.6;
 float cy=cos(uCam.x),sy=sin(uCam.x),cp=cos(uCam.y),sp=sin(uCam.y);
 float X=p.x*cy-p.z*sy, Z0=p.x*sy+p.z*cy, Y=p.y*cp+Z0*sp, Z=Z0*cp-p.y*sp, d=Z+uCam.z;
 vec2 s=vec2(uView.x+X*uCam.w/d, uView.y-Y*uCam.w/d);
 gl_Position=vec4(s.x/uRes.x*2.0-1.0, 1.0-s.y/uRes.y*2.0, 0.0, 1.0);
 gl_PointSize=max(1.0,3.2/d*uRes.y/700.0); vA=smoothstep(0.98,-0.5,p.y)*smoothstep(-0.6,-0.3,p.y);}`;
const PFS=`precision mediump float;varying float vA;uniform float uAlpha;
void main(){vec2 q=gl_PointCoord-0.5; float r=1.0-smoothstep(0.1,0.5,length(q)); gl_FragColor=vec4(0.95,0.64,0.23,r*vA*uAlpha);}`;
function mk(type,src){const s=gl.createShader(type); gl.shaderSource(s,src); gl.compileShader(s); if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(s); return s}
function program(vs,fs){const p=gl.createProgram(); gl.attachShader(p,mk(gl.VERTEX_SHADER,vs)); gl.attachShader(p,mk(gl.FRAGMENT_SHADER,fs)); gl.linkProgram(p);
  if(!gl.getProgramParameter(p,gl.LINK_STATUS)) throw gl.getProgramInfoLog(p); return p}
function initGL(){
  try{
    gl=glc.getContext('webgl',{antialias:true,premultipliedAlpha:false,powerPreference:'high-performance'});
    if(!gl) return false;
    prog=program(VS,FS); parts={prog:program(PVS,PFS)};
    buf.pos=gl.createBuffer(); buf.info=gl.createBuffer(); buf.idx=gl.createBuffer(); buf.lpos=gl.createBuffer(); buf.linfo=gl.createBuffer();
    const N=RM?0:(innerWidth<760?220:520), pp=new Float32Array(N*3);
    for(let i=0;i<N;i++){pp[i*3]=Math.random()*3.6-1.8; pp[i*3+1]=Math.random()*1.6-0.6; pp[i*3+2]=Math.random()*2.9-1.0}
    parts.buf=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,parts.buf); gl.bufferData(gl.ARRAY_BUFFER,pp,gl.STATIC_DRAW); parts.n=N;
    glc.addEventListener('webglcontextlost',e=>{e.preventDefault(); gl=null});
    return true;
  }catch(e){ console.warn('WebGL off:',e); gl=null; return false }
}
function buildMesh(){
  const R=rows.length, A=0.55/yspan, P=[],I=[],X=[], LP=[],LI=[];
  let base=0;
  for(let r=0;r<R;r++){
    const age=r/(R-1||1), z=zOf(r,R); let last=null;
    const hs=rows[r].map(c=>{if(c.v!=null) last=(c.v-ymid)*A; return last});
    const first=hs.find(h=>h!=null)??0;
    for(let k=0;k<60;k++){const h=hs[k]??first; P.push(xOf(k),h,z, xOf(k),-0.62,z); I.push(age,1,0, age,0,0)}
    for(let k=0;k<59;k++){const a=base+k*2; X.push(a,a+1,a+2, a+1,a+3,a+2)}
    base+=120;
    if(r<R-1){ for(let k=0;k<59;k++){const c0=rows[r][k],c1=rows[r][k+1]; if(c0.v==null||c1.v==null) continue;
        const bk=c0.b&&c1.b?1:0; LP.push(xOf(k),(c0.v-ymid)*A,z, xOf(k+1),(c1.v-ymid)*A,z); LI.push(age,1,bk, age,1,bk)} }
  }
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.pos); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(P),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.info); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(I),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,buf.idx); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,new Uint16Array(X),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.lpos); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(LP),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.linfo); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(LI),gl.STATIC_DRAW);
  nFill=X.length; buf.nLines=LP.length/3; meshDirty=false;
}
function attr(p,name,b,size){const l=gl.getAttribLocation(p,name); if(l<0) return; gl.bindBuffer(gl.ARRAY_BUFFER,b); gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l,size,gl.FLOAT,false,0,0)}
function uniCam(p,c){gl.uniform4f(gl.getUniformLocation(p,'uCam'),c.yw,c.pitch,c.dist,c.f*DPR); gl.uniform4f(gl.getUniformLocation(p,'uView'),c.ox*DPR,c.oy*DPR,0,0); gl.uniform2f(gl.getUniformLocation(p,'uRes'),W*DPR,H*DPR)}
function drawGL(c){
  gl.viewport(0,0,glc.width,glc.height); gl.clearColor(0,0,0,0); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  if(!rows) return; if(meshDirty) buildMesh();
  gl.useProgram(prog); uniCam(prog,c);
  gl.uniform3fv(gl.getUniformLocation(prog,'uA'),hex('#2E4068')); gl.uniform3fv(gl.getUniformLocation(prog,'uB'),hex('#A3B6DC')); gl.uniform3fv(gl.getUniformLocation(prog,'uV'),hex(C.violet));
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
  if(c.flat<1){
    gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.enable(gl.POLYGON_OFFSET_FILL); gl.polygonOffset(1,1);
    gl.uniform4f(gl.getUniformLocation(prog,'uMode'),0,c.focus,c.flat,0);
    attr(prog,'aPos',buf.pos,3); attr(prog,'aInfo',buf.info,3); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,buf.idx);
    gl.drawElements(gl.TRIANGLES,nFill,gl.UNSIGNED_SHORT,0);
    gl.disable(gl.POLYGON_OFFSET_FILL);
    gl.uniform4f(gl.getUniformLocation(prog,'uMode'),1,c.focus,c.flat,0);
    attr(prog,'aPos',buf.lpos,3); attr(prog,'aInfo',buf.linfo,3);
    gl.drawArrays(gl.LINES,0,buf.nLines);
    gl.disable(gl.DEPTH_TEST);
  }
  if(parts.n){
    gl.useProgram(parts.prog); uniCam(parts.prog,c);
    gl.uniform1f(gl.getUniformLocation(parts.prog,'uT'),performance.now()/1000);
    gl.uniform1f(gl.getUniformLocation(parts.prog,'uAlpha'),0.55*(1-c.down)+0.08);
    gl.blendFunc(gl.SRC_ALPHA,gl.ONE); attr(parts.prog,'aPos',parts.buf,3); gl.drawArrays(gl.POINTS,0,parts.n);
  }
}

// ---------------------------------------------------------------- 2D overlay: newest ridge, its morph into the proof, the recording's hum
const PN=PROOF.st.length, PST=PROOF.st.map(x=>50+x/1e4), PV=PROOF.v.map(x=>x==null?null:50+x/1e4);
function fitAt(shift){let n=0,sa=0,sb=0,saa=0,sbb=0,sab=0;
  for(let i=0;i<PN;i++){const j=i-shift; if(j<0||j>=PN||PV[j]==null) continue; const a=PST[i],b=PV[j]; n++;sa+=a;sb+=b;saa+=a*a;sbb+=b*b;sab+=a*b}
  if(n<20) return 0; const cov=sab/n-sa/n*sb/n; return cov/Math.sqrt((saa/n-(sa/n)**2)*(sbb/n-(sb/n)**2))}
const FITS={}; const fit=s=>FITS[s]??(FITS[s]=fitAt(s));
function draw2D(c,full){
  const ctx=cx; ctx.setTransform(DPR,0,0,DPR,0,0); ctx.clearRect(0,0,W,H); if(!rows) return;
  const proj=projector(c), R=rows.length, A=0.55/yspan, p=c.p;
  const L=c.mob?58:Math.max(90,W*0.08), Rr=W-(c.mob?16:60), Top=H*(c.mob?0.24:0.2), Bot=H*(c.mob?0.5:0.56);
  const plo=49.86,phi=50.36, py=v=>lerp(Bot,Top,(v-plo)/(phi-plo)), px=i=>lerp(L,Rr,i/(PN-1));
  ctx.lineJoin='round'; ctx.lineCap='round';
  for(let r=full?0:R-1;r<R;r++){
    const row=rows[r], newest=r===R-1, z=zOf(r,R), age=r/(R-1||1);
    const alpha=newest?1:lerp(1,0.07,c.focus)*(1-c.flat); if(alpha<=0.01) continue;
    const pts=row.map((q,k)=>q.v==null?null:proj(xOf(k),(q.v-ymid)*A,z));
    if(full&&!newest){ctx.globalAlpha=alpha; ctx.beginPath(); let s=false; pts.forEach(q=>{if(!q)return; s?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); s=true});
      const b1=proj(xOf(59),-0.62,z),b0=proj(xOf(0),-0.62,z); ctx.lineTo(b1[0],b1[1]); ctx.lineTo(b0[0],b0[1]); ctx.closePath(); ctx.fillStyle=C.deep; ctx.globalAlpha=alpha*0.9; ctx.fill();
      ctx.globalAlpha=alpha; ctx.beginPath(); let pen=false; pts.forEach(q=>{if(!q){pen=false;return} pen?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); pen=true});
      ctx.strokeStyle=`rgba(124,141,176,${0.25+0.75*Math.pow(age,1.6)})`; ctx.lineWidth=1.2; ctx.stroke(); continue}
    ctx.globalAlpha=1;
    if(c.flat>0){ctx.beginPath();
      for(let i=0;i<PN;i++){const k=Math.round(i/(PN-1)*59), q=pts[k]||proj(xOf(k),0,z); const X=lerp(q[0],px(i),c.flat), Y=lerp(q[1],py(PST[i]),c.flat); i?ctx.lineTo(X,Y):ctx.moveTo(X,Y)}
      ctx.strokeStyle=C.sodium; ctx.lineWidth=2.6; ctx.shadowColor='rgba(242,162,58,.55)'; ctx.shadowBlur=14; ctx.stroke(); ctx.shadowBlur=0; continue}
    ctx.beginPath(); let s=false; pts.forEach(q=>{if(!q)return; s?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); s=true});
    const b1=proj(xOf(59),-0.62,z),b0=proj(xOf(0),-0.62,z); ctx.lineTo(b1[0],b1[1]); ctx.lineTo(b0[0],b0[1]); ctx.closePath();
    let ytop=1e9,ybot=Math.max(b0[1],b1[1]); pts.forEach(q=>{if(q&&q[1]<ytop) ytop=q[1]});
    const g=ctx.createLinearGradient(0,ytop,0,ybot); g.addColorStop(0,'rgba(242,162,58,.14)'); g.addColorStop(0.22,'rgba(14,23,41,.94)'); g.addColorStop(1,'rgba(10,17,32,.97)'); ctx.fillStyle=g; ctx.fill();
    ctx.beginPath(); let pen=false; pts.forEach(q=>{if(!q){pen=false;return} pen?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); pen=true});
    ctx.strokeStyle=C.sodium; ctx.lineWidth=2.6; ctx.shadowColor='rgba(242,162,58,.6)'; ctx.shadowBlur=16; ctx.stroke(); ctx.shadowBlur=0;
    ctx.beginPath(); pen=false; row.forEach((q,k)=>{const P=pts[k]; if(!P||!q.b){pen=false;return} pen?ctx.lineTo(P[0],P[1]):ctx.moveTo(P[0],P[1]); pen=true});
    ctx.strokeStyle=C.violet; ctx.lineWidth=2.6; ctx.stroke();
    let k=59; while(k>0&&!pts[k]) k--; const q=pts[k];
    if(q){const t=performance.now()/1000, rr=RM?5:5+2*Math.sin(t*3.3); ctx.fillStyle=C.sodium; ctx.beginPath(); ctx.arc(q[0],q[1],rr,0,7); ctx.fill();
      if(!RM){ctx.strokeStyle=`rgba(242,162,58,${0.5-(t%1.6)/3.2})`; ctx.lineWidth=1.5; ctx.beginPath(); ctx.arc(q[0],q[1],6+(t%1.6)*14,0,7); ctx.stroke()}}
  }
  const hum=seg(p,0.62,0.7);
  if(hum>0){
    const slide=ease(seg(p,0.8,0.95)), shift=Math.round(lerp(55,0,slide));
    ctx.globalAlpha=hum; ctx.fillStyle=C.cyan;
    for(let i=0;i<PN;i++){const j=i-shift; if(j<0||j>=PN||PV[j]==null||PV[j]<plo||PV[j]>phi) continue; ctx.beginPath(); ctx.arc(px(i),py(PV[j]),1.7,0,7); ctx.fill()}
    ctx.globalAlpha=hum*0.9; ctx.font='13px "IBM Plex Sans",system-ui'; ctx.textAlign='right';
    [49.9,50.0,50.1,50.2,50.3].forEach(v=>{const y=py(v); ctx.fillStyle=C.mut; ctx.fillText(v.toFixed(1)+' Hz',L-8,y+4); ctx.fillStyle='rgba(132,148,178,.18)'; ctx.fillRect(L,y,Rr-L,1)});
    ctx.fillStyle=C.mut;
    for(let m=0;m<=50;m+=(c.mob?20:10)){const i=Math.round(m*60/PROOF.dt); if(i>=PN)break; ctx.textAlign=m===0?'left':'center'; ctx.fillText(PKT.format((PROOF.t0+i*PROOF.dt)*1000),px(i),Bot+22)}
    const ft=fit(shift); ctx.textAlign='right'; ctx.font='600 15px "IBM Plex Sans",system-ui'; ctx.fillStyle=ft>0.8?C.ink:C.mut;
    ctx.fillText(shift?`Recording ${shift*PROOF.dt} s out of place · fit ${ft.toFixed(2)}`:`Locked on · fit ${ft.toFixed(2)}`,Rr,Top-16);
    ctx.globalAlpha=1;
  }
}

// ---------------------------------------------------------------- scroll state and frame loop
const state={p:0};
let lastStep=-1;
function size(){DPR=Math.min(2,devicePixelRatio||1); W=glc.clientWidth; H=glc.clientHeight; for(const c of [glc,fx]){c.width=W*DPR; c.height=H*DPR}}
function frame(){
  const c=camAt(state.p), p=state.p;
  if(gl) drawGL(c); draw2D(c,!gl);
  if(!G) $('hero').style.opacity=1-seg(p,0.05,0.12);
  $('axis').style.opacity=1-seg(p,0.3,0.4); $('live').style.opacity=1-seg(p,0.5,0.58);
  const st=p<0.14?0:p<0.36?1:p<0.58?2:p<0.78?3:4;
  if(st!==lastStep){lastStep=st; showStep(st)}
  $('legend').classList.toggle('on',p>0.58&&p<0.99);
  requestAnimationFrame(frame);
}
function showStep(st){
  document.querySelectorAll('.step').forEach(e=>{const on=+e.dataset.s===st; e.classList.toggle('on',on);
    if(on&&G&&!RM){const ls=e._lines||e.querySelectorAll('h2,p,.n'); G.fromTo(ls,{yPercent:60,opacity:0,filter:'blur(6px)'},{yPercent:0,opacity:1,filter:'blur(0px)',duration:.7,stagger:.06,ease:'power3.out',overwrite:true})}});
}

// ---------------------------------------------------------------- proof chart
const pc=$('proof'), pctx=pc.getContext('2d'); let PW=0,PH=0;
function sizeProof(){PW=pc.clientWidth; PH=pc.clientHeight; pc.width=PW*DPR; pc.height=PH*DPR}
const PM={l:62,r:18,t:12,b:34}, plo=49.86, phi=50.36;
const gx=i=>PM.l+(PW-PM.l-PM.r)*i/(PN-1), gy=v=>PM.t+(PH-PM.t-PM.b)*(1-(v-plo)/(phi-plo));
let hoverI=null; const rev={v:RM?1:0};
function drawProof(){
  const c=pctx, rv=rev.v; c.setTransform(DPR,0,0,DPR,0,0); c.clearRect(0,0,PW,PH);
  c.font='12px "IBM Plex Sans",system-ui'; c.textAlign='right';
  [49.9,50.0,50.1,50.2,50.3].forEach(v=>{const y=gy(v); c.fillStyle=C.mut; c.fillText(v.toFixed(1)+' Hz',PM.l-10,y+4); c.fillStyle='rgba(132,148,178,.14)'; c.fillRect(PM.l,y,PW-PM.l-PM.r,1)});
  c.fillStyle=C.mut;
  for(let m=0;m<=50;m+=(PW<600?20:10)){const i=Math.round(m*60/PROOF.dt); if(i>=PN)break; c.textAlign=m===0?'left':'center'; c.fillText(PKT.format((PROOF.t0+i*PROOF.dt)*1000),gx(i),PH-10)}
  const a=PROOF.cut[0]/PROOF.dt, b=PROOF.cut[1]/PROOF.dt; c.fillStyle='rgba(164,147,255,.16)'; c.fillRect(gx(a),PM.t,(gx(b)-gx(a))*clamp(rv*1.4,0,1),PH-PM.t-PM.b);
  const n=Math.floor(PN*rv); c.save(); c.beginPath(); c.rect(0,0,gx(Math.max(0,n-1))+1,PH); c.clip();
  c.beginPath(); for(let i=0;i<PN;i++){i?c.lineTo(gx(i),gy(PST[i])):c.moveTo(gx(i),gy(PST[i]))}
  c.strokeStyle=C.sodium; c.lineWidth=2.4; c.lineJoin='round'; c.stroke();
  c.fillStyle=C.cyan; for(let i=0;i<PN;i++){if(PV[i]==null||PV[i]<plo||PV[i]>phi)continue; c.beginPath(); c.arc(gx(i),gy(PV[i]),1.6,0,7); c.fill()}
  c.restore();
  if(n>1&&rv<1){c.fillStyle=C.sodium; c.beginPath(); c.arc(gx(n-1),gy(PST[n-1]),4,0,7); c.fill()}
  if(hoverI!=null&&rv>=1){const x=gx(hoverI); c.fillStyle='rgba(234,240,248,.35)'; c.fillRect(x,PM.t,1,PH-PM.t-PM.b);
    c.fillStyle=C.sodium; c.beginPath(); c.arc(x,gy(PST[hoverI]),4.5,0,7); c.fill();
    if(PV[hoverI]!=null){c.strokeStyle=C.deep;c.lineWidth=2;c.fillStyle=C.cyan; c.beginPath(); c.arc(x,gy(PV[hoverI]),4.5,0,7); c.fill(); c.stroke()}}
}
pc.addEventListener('pointermove',e=>{const r=pc.getBoundingClientRect(), x=e.clientX-r.left, i=Math.round((x-PM.l)/(PW-PM.l-PM.r)*(PN-1)), tip=$('ptip');
  if(i<0||i>=PN){hoverI=null;tip.style.display='none';drawProof();return}
  hoverI=i; drawProof();
  const t=(PROOF.t0+i*PROOF.dt)*1000, inCut=i*PROOF.dt>=PROOF.cut[0]&&i*PROOF.dt<=PROOF.cut[1];
  tip.innerHTML=`<b>${PKTs.format(t)}</b><br><span style="color:${C.sodium}">Station ${PST[i].toFixed(3)} Hz</span><br><span style="color:${C.cyan}">Video ${PV[i]==null?'lost the hum here':PV[i].toFixed(3)+' Hz'}</span>${inCut?'<br><span style="color:#A493FF">mains off, inverter</span>':''}`;
  tip.style.display='block'; const w=tip.offsetWidth; let L=x+34; if(L+w>pc.clientWidth) L=x-w-4; tip.style.left=L+'px'; tip.style.top='20px'});
pc.addEventListener('pointerleave',()=>{hoverI=null;$('ptip').style.display='none';drawProof()});

// ---------------------------------------------------------------- motion: Lenis + ScrollTrigger + SplitText
function motion(){
  if(!G||!ST){
    addEventListener('scroll',()=>{const s=$('seq'),r=s.getBoundingClientRect(); state.p=clamp(-r.top/(s.offsetHeight-innerHeight),0,1)},{passive:true});
    new IntersectionObserver((es,ob)=>{if(es[0].isIntersecting){ob.disconnect(); rev.v=1; drawProof()}},{threshold:.35}).observe(pc);
    return;
  }
  G.registerPlugin(ST); if(SPLIT) G.registerPlugin(SPLIT);
  if(window.Lenis&&!RM){
    const lenis=new Lenis({lerp:0.09,smoothWheel:true});
    lenis.on('scroll',ST.update); G.ticker.add(t=>lenis.raf(t*1000)); G.ticker.lagSmoothing(0);
    document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',e=>{const t=document.querySelector(a.getAttribute('href')); if(t){e.preventDefault(); lenis.scrollTo(t,{duration:1.6})}}));
  }
  G.to(state,{p:1,ease:'none',scrollTrigger:{trigger:'#seq',start:'top top',end:'bottom bottom',scrub:RM?true:0.9}});
  G.to('#hero',{yPercent:-18,opacity:0,ease:'none',scrollTrigger:{trigger:'#seq',start:'top top',end:'12% top',scrub:true}});
  ST.create({trigger:'#seq',start:'10% top',onToggle:s=>{$('hero').style.pointerEvents=s.isActive?'none':'auto'}});
  if(RM){rev.v=1; drawProof(); return}
  if(SPLIT){
    const sp=new SplitText('#hero h1',{type:'words,chars',wordsClass:'w'});
    G.timeline({defaults:{ease:'expo.out'}})
      .from(sp.chars,{yPercent:110,rotateX:-80,opacity:0,duration:1.1,stagger:{each:0.018,from:'start'}},0.15)
      .from('#hero p',{y:24,opacity:0,duration:.9},0.55)
      .from('#hero .btn',{y:18,opacity:0,duration:.7,stagger:.08},0.7)
      .from('#live',{x:30,opacity:0,duration:.9},0.4)
      .from('#axis',{opacity:0,duration:1},0.8)
      .from('.stage canvas',{opacity:0,duration:2.2,ease:'power2.out'},0)
      .from('.nav',{y:-30,opacity:0,duration:.8},0.2);
    document.querySelectorAll('.step').forEach(s=>{const parts=new SplitText(s.querySelector('h2'),{type:'lines',linesClass:'ln'}); s._lines=[s.querySelector('.n'),...parts.lines,s.querySelector('p')]});
  }
  G.utils.toArray('.block h2').forEach(h=>{const s=SPLIT?new SplitText(h,{type:'lines',linesClass:'ln'}).lines:[h];
    G.from(s,{yPercent:100,opacity:0,duration:1,stagger:.1,ease:'expo.out',scrollTrigger:{trigger:h,start:'top 82%'}})});
  G.utils.toArray('.figs li').forEach((li,i)=>{const b=li.querySelector('b'), m=b.textContent.match(/^([<]?)([\d.]+)(.*)$/);
    G.from(li,{y:40,opacity:0,duration:.9,delay:i*.08,ease:'power3.out',scrollTrigger:{trigger:'.figs',start:'top 85%'}});
    if(m&&!m[3].startsWith(':')){const v=parseFloat(m[2]), dp=(m[2].split('.')[1]||'').length, o={x:0}; b.textContent=m[1]+(0).toFixed(dp)+m[3];
      G.to(o,{x:v,duration:1.6,delay:.2+i*.08,ease:'power2.out',scrollTrigger:{trigger:'.figs',start:'top 85%'},onUpdate:()=>{b.textContent=m[1]+o.x.toFixed(dp)+m[3]}})}});
  G.to(rev,{v:1,duration:2.4,ease:'power2.inOut',onUpdate:drawProof,scrollTrigger:{trigger:pc,start:'top 75%'}});
  G.from('.chartwrap',{clipPath:'inset(0 0 100% 0 round 18px)',duration:1.2,ease:'expo.inOut',scrollTrigger:{trigger:'.chartwrap',start:'top 85%'}});
  G.utils.toArray('.fine li').forEach((li,i)=>G.from(li,{x:40,opacity:0,duration:.8,delay:i*.1,ease:'power3.out',scrollTrigger:{trigger:'.fine',start:'top 85%'}}));
}

addEventListener('resize',()=>{size();sizeProof();drawProof(); if(ST) ST.refresh()});
size(); sizeProof(); initGL(); motion(); drawProof();
loadPulse(); setInterval(loadPulse,20000); setInterval(showLive,5000);
requestAnimationFrame(frame);
})();

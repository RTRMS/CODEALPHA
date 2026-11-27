// AnnSeva frontend — single source of truth (previously duplicated between
// index.html's inline <script> and this file, which index.html never loaded).

// The FastAPI server serves this page on port 8000. If the page is opened
// directly or through VS Code Live Server, still send API requests to FastAPI.
const API = (location.protocol === "file:" || ["5500","5501","5502"].includes(location.port))
  ? "http://127.0.0.1:8000" : "";
const state = {
  role: "farmer", page: "dashboard", mlResults: null, adminId: null,
  farmer: {id:"FMR-1042", name:"Ravi Kumar"},
  farmers: [], applications: [], requirement: {}, notifications: [],
  loading: true, token: localStorage.getItem("annseva_token") || null
};

const esc = v => String(v ?? "").replace(/[&<>\"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = (a,b) => b > 0 ? Math.min(100, Math.max(0, Math.round((Number(a)||0)/b*100))) : 0;
function toast(msg){const t=document.getElementById("toast");t.textContent=msg;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2400)}
function farmerApplications(){return state.applications.filter(a=>a.farmer_id===state.farmer.id)}
function currentFarmer(){
  const apps=farmerApplications(); const a=apps[0];
  return {...state.farmer,
    crop:a?.crop||"—", qty:a?.quantity||0, status:a?.status||"No submission",
    allocation:a?.allocation||0, centre:a?.centre||"Awaiting allocation",
    date:a?.schedule_date||"—", slot:a?.schedule_slot||"—", token:a?.token||"—",
    applicationId:a?.application_id||"—", scheduled:!!a?.schedule_date
  };
}
async function api(path, options={}){
  const headers={"Content-Type":"application/json",...(options.headers||{})};
  if(state.token) headers.Authorization=`Bearer ${state.token}`;
  const r=await fetch(API+path,{...options,headers});
  let data=null; try{data=await r.json()}catch{}
  if(!r.ok){
    // A 401 can happen after a database reset or an expired session.
    // Clear the stale browser token and immediately show the sign-in dialog
    // instead of leaving the user stuck re-seeing this error on every click.
    if(r.status===401){
      localStorage.removeItem("annseva_token");
      state.token=null; state.adminId=null;
      const overlay=document.getElementById("loginOverlay");
      if(overlay) overlay.style.display="flex";
    }
    throw new Error(data?.detail||`HTTP ${r.status}`);
  }
  return data;
}
function toggleFarmerId(){
 const isFarmer=document.getElementById("loginRole").value==="farmer";
 document.getElementById("farmerIdWrap").style.display=isFarmer?"block":"none";
 document.getElementById("adminIdWrap").style.display=isFarmer?"none":"block";
}
async function login(e){
 e.preventDefault();
 try{
  const role=document.getElementById("loginRole").value, farmer_id=role==="farmer"?document.getElementById("loginFarmerId").value.trim():null;
  const admin_id=role==="admin"?document.getElementById("loginAdminId").value:null;
  const data=await api("/api/login",{method:"POST",body:JSON.stringify({role,farmer_id,admin_id,password:document.getElementById("loginPassword").value})});
  state.token=data.token; state.adminId=data.admin_id||null; localStorage.setItem("annseva_token",state.token); state.role=data.role; state.farmer.id=data.farmer_id||"FMR-1042"; state.page=state.role==="admin"?"adminDashboard":"dashboard";
  document.getElementById("loginOverlay").style.display="none"; await refreshState(true);
 }catch(err){toast(err.message)}
}
async function signOut(){try{await api("/api/logout",{method:"POST"})}catch{} localStorage.removeItem("annseva_token");state.token=null;state.adminId=null;document.getElementById("loginOverlay").style.display="flex"}
async function refreshState(silent=false){
  try{
    const data=await api("/api/state");
    state.farmers=data.farmers||[]; state.applications=data.applications||[];
    state.requirement=data.requirement||{}; state.notifications=data.notifications||[];
    const me=state.farmers.find(f=>f.id===state.farmer.id); if(me) state.farmer={...state.farmer,name:me.name};
    state.loading=false;
    // Polling every few seconds must NOT rebuild an active form. Re-rendering
    // the page replaces the <select>/<input> DOM nodes and makes typed values
    // disappear while the user is entering them.
    // Never replace an active form DOM while the user may be typing.
    // Replacing the DOM destroys the current input values/focus.
    const activeFormPage = ['submit','requirements'].includes(state.page);
    const activeElement = document.activeElement;
    const typingInForm = !!activeElement && !!activeElement.closest && !!activeElement.closest('form');
    if (!activeFormPage && !typingInForm) render();
  }catch(e){
    state.loading=false;
    // Keep an active form untouched if the background poll temporarily fails.
    // Otherwise a failed poll would rebuild the form and clear what the user typed.
    const activeElement = document.activeElement;
    const activeFormPage = ['submit','requirements'].includes(state.page);
    const typingInForm = !!activeElement && !!activeElement.closest && !!activeElement.closest('form');
    if (!activeFormPage && !typingInForm) render();
    if(!silent && e?.message!=="Session expired or invalid" && e?.message!=="Authentication required") toast("Backend unavailable. Start: python server.py");
  }
}
function layout(){
 document.getElementById("topTitle").textContent=state.role==="farmer"?"Farmer Dashboard":"Admin Dashboard";
 document.getElementById("farmerBtn").classList.toggle("active",state.role==="farmer");
 document.getElementById("adminBtn").classList.toggle("active",state.role==="admin");
 const f=currentFarmer(); document.getElementById("userName").textContent=state.role==="farmer"?f.name:"Procurement Officer";
 document.getElementById("avatar").textContent=state.role==="farmer"?(f.name?.[0]||"R"):"A";
 document.getElementById("nav").innerHTML=state.role==="farmer"?`
  <button data-page="dashboard">⌂ &nbsp; Dashboard</button><button data-page="submit">＋ &nbsp; Submit Crop</button>
  <button data-page="applications">▣ &nbsp; Applications</button><button data-page="schedule">◷ &nbsp; My Schedule</button>
  <button data-page="notifications">● &nbsp; Notifications</button>`:`
  <button data-page="adminDashboard">⌂ &nbsp; Overview</button><button data-page="requirements">＋ &nbsp; Requirements</button>
  <button data-page="farmers">♙ &nbsp; Farmers</button><button data-page="recommendations">✦ &nbsp; Recommendations</button>
  <button data-page="scheduling">◷ &nbsp; Scheduling</button><button data-page="capacity">▤ &nbsp; Centre Capacity</button>`;
 document.querySelectorAll("#nav button").forEach(b=>b.onclick=()=>{state.page=b.dataset.page;render()});
 document.querySelectorAll("#nav button").forEach(b=>b.classList.toggle("active",b.dataset.page===state.page));
}
function farmerDashboard(){
 const f=currentFarmer(); const ready=f.status==="Scheduled"; const approved=["Approved","Scheduled","Completed"].includes(f.status);
 return `<div class="page-head"><div><h2>Welcome, ${esc(f.name.split(" ")[0])}</h2><div class="muted">Track your crop procurement journey from one place.</div></div><button class="btn primary" onclick="state.page='submit';render()">Submit New Crop</button></div>
 <div class="hero"><h2>Procurement status: <span class="pill ${ready?'info':approved?'success':'warning'}">${esc(f.status)}</span></h2>
 <p>${f.status==="No submission"?"Submit a crop application to start the procurement journey.":f.status==="Submitted"?"Your application is recorded and waiting for admin/ML review.":f.status==="Approved"?`Your ${esc(f.crop)} application is approved for ${f.allocation} qtl. Scheduling is next.`:`Your ${esc(f.crop)} procurement is scheduled.`}</p>
 ${ready?`<button class="btn secondary" onclick="state.page='schedule';render()">View Schedule →</button>`:`<button class="btn secondary" onclick="state.page='applications';render()">View Applications →</button>`}</div>
 <div class="grid stats" style="margin-top:16px"><div class="stat"><div class="label">Submitted quantity</div><div class="value">${f.qty||0} qtl</div><div class="trend">${esc(f.applicationId)}</div></div>
 <div class="stat"><div class="label">Allocated quantity</div><div class="value">${f.allocation||0} qtl</div><div class="trend">${pct(f.allocation,f.qty)}% allocated</div></div>
 <div class="stat"><div class="label">Procurement centre</div><div class="value" style="font-size:18px">${esc(f.centre)}</div><div class="trend">Capacity-aware allocation</div></div>
 <div class="stat"><div class="label">Token</div><div class="value">${esc(f.token)}</div><div class="trend">${esc(f.date)} · ${esc(f.slot)}</div></div></div>
 <div class="grid two" style="margin-top:16px"><div class="card"><div class="section-title">Procurement journey</div><div class="timeline">
 ${[["Application submitted",true,"Crop and quantity recorded in the system."],["Requirement matched",approved||f.status!=="Submitted","System matching and admin review."],["Admin approved",approved,"Recommendation reviewed and quantity approved."],["Scheduled for procurement",ready,"A centre, token and time slot are assigned."]].map((x,i)=>`<div class="step"><div class="dot">${x[1]?"✓":i+1}</div><div><h4>${x[0]}</h4><p>${x[2]}</p></div></div>`).join("")}
 </div></div><div class="card"><div class="section-title">Latest notifications</div>${state.notifications.slice(0,4).map(n=>`<div class="notification"><div class="icon">●</div><div><b>${esc(n.title)}</b><div class="muted">${esc(n.message)}</div></div></div>`).join("")||'<div class="empty">No notifications yet.</div>'}</div></div>`;
}
function submitPage(){return `<div class="page-head"><div><h2>Register Crop Availability</h2><div class="muted">Register your crop once. AnnSeva will automatically match it to future requirements for the same crop.</div></div></div><div class="grid two"><div class="card"><div class="section-title">Crop & quantity submission</div><form class="form" onsubmit="submitCrop(event)"><label>Crop<select id="crop"><option>Wheat</option><option>Rice</option><option>Maize</option><option>Barley</option></select></label><label>Available quantity (quintals)<input id="qty" type="number" min="1" step="0.01" required></label><label>Preferred location<input id="loc" required></label><div class="grid two"><label>Latitude (optional)<input id="lat" type="number" step="any" min="-90" max="90"></label><label>Longitude (optional)<input id="lon" type="number" step="any" min="-180" max="180"></label></div><button class="btn primary">Submit Procurement Application</button></form></div><div class="card"><div class="section-title">What happens next?</div><div class="timeline"><div class="step"><div class="dot">1</div><div><h4>Crop availability recorded</h4><p>Your farmer, crop and quantity are stored in SQLite as a standing offer.</p></div></div><div class="step"><div class="dot">2</div><div><h4>Automated matching</h4><p>The ML service checks crop, quantity, location and verification.</p></div></div><div class="step"><div class="dot">3</div><div><h4>Admin validation</h4><p>An officer approves the application and quantity.</p></div></div><div class="step"><div class="dot">4</div><div><h4>Scheduling</h4><p>A unique procurement slot and token are assigned.</p></div></div></div></div></div>`}
function applicationsPage(){return `<div class="page-head"><div><h2>My Applications</h2><div class="muted">Live crop submissions from the AnnSeva database. Every crop you submit appears here.</div></div><button class="btn outline" onclick="refreshState(true)">↻ Refresh</button></div><div class="card"><table><thead><tr><th>Application</th><th>Crop</th><th>Submitted</th><th>Allocated</th><th>Assigned Admin</th><th>Status</th><th>Schedule</th></tr></thead><tbody>${farmerApplications().map(a=>{const displayStatus=a.requirement_id==null&&a.status==='Registered'?'Registered — waiting for matching requirement':a.status;return `<tr><td><b>${esc(a.application_id)}</b></td><td>${esc(a.crop)}</td><td>${a.quantity} qtl</td><td>${a.allocation} qtl</td><td><span class="pill info">${esc(a.assigned_admin_id||'Pending')}</span></td><td><span class="pill ${a.status==='Approved'||a.status==='Scheduled'?'success':a.status==='Completed'?'info':'warning'}">${esc(displayStatus)}</span></td><td>${esc(a.schedule_date||'—')} · ${esc(a.schedule_slot||'—')}</td></tr>`}).join("")||'<tr><td colspan="7" class="empty">No applications yet.</td></tr>'}</tbody></table></div>`}
function schedulePage(){const f=currentFarmer(); return `<div class="page-head"><div><h2>My Procurement Schedule</h2><div class="muted">Your assigned centre and time slot.</div></div></div><div class="grid three"><div class="stat"><div class="label">Centre</div><div class="value" style="font-size:19px">${esc(f.centre)}</div></div><div class="stat"><div class="label">Date & time</div><div class="value" style="font-size:21px">${esc(f.date)}</div><div class="kpi-note">${esc(f.slot)}</div></div><div class="stat"><div class="label">Token</div><div class="value">${esc(f.token)}</div><div class="kpi-note">${f.allocation} qtl ${esc(f.crop)}</div></div></div><div class="card" style="margin-top:16px"><div class="section-title">Visit checklist</div><div class="grid three"><div><span class="pill ${f.allocation>0?'success':'warning'}">${f.allocation>0?'✓ Confirmed':'Pending'}</span><p>Procurement allocation</p></div><div><span class="pill ${f.scheduled?'success':'warning'}">${f.scheduled?'✓ Confirmed':'Pending'}</span><p>Centre & slot</p></div><div><span class="pill warning">Pending</span><p>Final centre verification</p></div></div></div>`}
function notificationsPage(){return `<div class="page-head"><div><h2>Notifications</h2><div class="muted">Live status updates from the procurement workflow.</div></div></div><div class="card">${state.notifications.map(n=>`<div class="notification"><div class="icon">●</div><div><b>${esc(n.title)}</b><div class="muted">${esc(n.message)}</div><small class="muted">${esc(n.created_at)}</small></div></div>`).join("")||'<div class="empty">No notifications yet.</div>'}</div>`}
function adminDashboard(){const r=state.requirement,pending=state.applications.filter(x=>x.requirement_id===r.id&&x.status==='Submitted').length;return `<div class="page-head"><div><h2>Procurement Overview</h2><div class="muted">Manage requirements, recommendations, allocations and centre capacity.</div></div><button class="btn primary" onclick="state.page='requirements';render()">Create Requirement</button></div><div class="grid stats"><div class="stat"><div class="label">Enlisted farmers</div><div class="value">${state.farmers.length}</div><div class="trend">Live database records</div></div><div class="stat"><div class="label">Pending applications</div><div class="value">${pending}</div><div class="trend">Needs review</div></div><div class="stat"><div class="label">Quantity required</div><div class="value">${r.needed||0} qtl</div><div class="trend">${esc(r.crop)}</div></div><div class="stat"><div class="label">Centre capacity</div><div class="value">${r.allocated||0}/${r.capacity||0} qtl</div><div class="trend">${pct(r.allocated,r.capacity)}% utilised</div></div></div><div class="grid two" style="margin-top:16px"><div class="card"><div class="section-title">Current requirement</div><h3>${esc(r.crop)} · ${r.needed||0} qtl</h3><p class="muted">${esc(r.centre)}</p><div class="progress"><i style="width:${pct(r.allocated,r.capacity)}%"></i></div><p class="kpi-note">${r.allocated||0} qtl allocated of ${r.capacity||0} qtl centre capacity.</p><button class="btn secondary" onclick="state.page='recommendations';render()">Review recommendations →</button></div><div class="card"><div class="section-title">Implementation flow</div><div class="grid" style="gap:10px"><div>1. Farmer submission <span class="pill success">Active</span></div><div>2. SQLite storage <span class="pill success">Active</span></div><div>3. Requirement creation <span class="pill success">Active</span></div><div>4. Non-local admin assignment <span class="pill info">Automatic</span></div><div>5. ML matching <span class="pill info">Ready</span></div><div>6. Assigned admin approval <span class="pill warning">Review</span></div><div>7. Scheduling <span class="pill warning">Next</span></div></div></div></div>`}
function requirementsPage(){const r=state.requirement;return `<div class="page-head"><div><h2>Create Procurement Requirement</h2><div class="muted">Define demand, centre capacity and per-slot processing capacity.</div></div></div><div class="grid two"><div class="card"><form class="form" onsubmit="createRequirement(event)"><label>Required crop<select id="reqCrop"><option>Wheat</option><option>Rice</option><option>Maize</option><option>Barley</option></select></label><label>Required quantity (quintals)<input id="reqQty" type="number" value="${r.needed||300}" min="1" step="0.01" required></label><label>Procurement centre<input id="reqCentre" value="${esc(r.centre||'Kurnool Procurement Centre')}" required></label><div class="grid two"><label>Centre latitude<input id="reqLat" type="number" step="any" min="-90" max="90"></label><label>Centre longitude<input id="reqLon" type="number" step="any" min="-180" max="180"></label></div><div class="grid two"><label>Centre capacity (quintals)<input id="reqCap" type="number" value="${r.capacity||500}" min="1" step="0.01" required></label><label>Slot capacity (quintals)<input id="reqSlotCap" type="number" value="${r.slot_capacity||50}" min="1" step="0.01" required></label></div><button class="btn primary">Save Requirement & Run Matching</button></form></div><div class="card"><div class="section-title">Requirement preview</div><h3>${esc(r.crop)}</h3><p>${r.needed||0} qtl required</p><p class="muted">${esc(r.centre)}</p><p class="kpi-note">${r.slot_capacity||50} qtl maximum per 30-minute slot.</p><div class="progress"><i style="width:${pct(r.allocated,r.capacity)}%"></i></div><p class="kpi-note">Capacity utilisation: ${pct(r.allocated,r.capacity)}%</p></div></div>`}
function farmersPage(){return `<div class="page-head"><div><h2>Enlisted Farmers</h2><div class="muted">Live farmer/application records.</div></div><button class="btn outline" onclick="refreshState()">↻ Refresh</button></div><div class="card"><table><thead><tr><th>Farmer</th><th>Crop</th><th>Available</th><th>Location</th><th>Verification</th><th>Score</th><th>Status</th><th>Action</th></tr></thead><tbody>${state.farmers.map(f=>`<tr><td><b>${esc(f.name)}</b><div class="muted">${esc(f.id)}</div></td><td>${esc(f.crop)}</td><td>${f.qty} qtl</td><td>${esc(f.location)}</td><td><span class="pill ${f.verified?'success':'warning'}">${f.verified?'Verified':'Pending'}</span></td><td><b>${f.score}%</b></td><td><span class="pill ${f.status==='Approved'||f.status==='Scheduled'?'success':f.status==='Completed'?'info':'warning'}">${esc(f.status)}</span></td><td>${f.verified?'<span class="muted">Verified</span>':`<button class="btn secondary" onclick="verifyFarmer('${esc(f.id)}')">Verify</button>`}</td></tr>`).join("")}</tbody></table></div>`}
function recommendationsPage(){const results=state.mlResults,recs=results?.recommendations||[],allocs=results?.allocations||[];return `<div class="page-head"><div><h2>Farmer Recommendations</h2><div class="muted">ML ranks eligible submitted applications.</div></div><div style="display:flex;gap:8px"><button class="btn outline" onclick="resetDemo()">Reset Demo</button><button class="btn primary" onclick="runMLMatching()">Run ML Matching</button></div></div><div id="mlStatus" class="card" style="margin-bottom:16px;background:#f8fbf9">${results?`<b>✓ ML recommendations generated</b><div class='muted'>The engine uses a persisted Random Forest only after 20+ real completion outcomes; otherwise it transparently uses the cold-start baseline.</div>`:`<b>ML engine ready</b><div class='muted'>Click “Run ML Matching” to generate live recommendations.</div>`}</div><div class="grid stats"><div class="stat"><div class="label">Recommended farmers</div><div class="value">${recs.length||'—'}</div></div><div class="stat"><div class="label">Requirement</div><div class="value">${state.requirement.needed||0} qtl</div><div class="trend">${esc(state.requirement.crop)}</div></div><div class="stat"><div class="label">Centre capacity</div><div class="value">${state.requirement.capacity||0} qtl</div><div class="trend">${state.requirement.allocated||0} qtl already allocated</div></div><div class="stat"><div class="label">Recommended allocation</div><div class="value">${allocs.reduce((s,a)=>s+Number(a.recommended_quantity||0),0)||'—'} qtl</div></div></div><div class="card" style="margin-top:16px"><div class="section-title">ML-ranked applications</div><div id="recommendationTable">${recs.length?`<table><thead><tr><th>Rank</th><th>Farmer</th><th>Crop</th><th>Available</th><th>Location</th><th>ML score</th><th>Assigned Admin</th><th>Status</th><th>Action</th></tr></thead><tbody>${recs.map((f,i)=>{const live=state.farmers.find(x=>x.id===f.farmer_id);const a=state.applications.find(x=>x.application_id===f.application_id);const status=a?.status||live?.status||'Submitted';return `<tr><td><b>#${i+1}</b></td><td><b>${esc(f.name)}</b><div class="muted">${esc(f.farmer_id)}</div></td><td>${esc(f.crop)}</td><td>${f.available_quantity} qtl</td><td>${esc(f.location)}</td><td><span class="pill success">${f.match_score}%</span></td><td><span class="pill info">${esc(f.assigned_admin_id||'Pending assignment')}</span></td><td><span class="pill ${status==='Approved'||status==='Scheduled'?'success':'warning'}">${esc(status)}</span></td><td>${status==='Submitted'?`<button class="btn secondary" onclick="approve('${esc(f.application_id)}')">Approve</button>`:status==='Approved'?'<span class="pill success">✓ Approved</span>':'<span class="pill info">Scheduled</span>'}</td></tr>`}).join('')}</tbody></table>`:'<div class="empty">Run ML Matching to populate recommendations.</div>'}</div></div><div class="card" style="margin-top:16px"><div class="section-title">Recommended allocation</div>${allocs.length?`<table><thead><tr><th>Farmer</th><th>ML score</th><th>Recommended quantity</th><th>Centre</th><th>Approval</th></tr></thead><tbody>${allocs.map(a=>{const app=state.applications.find(x=>x.application_id===a.application_id);return `<tr><td><b>${esc(a.name)}</b><div class="muted">${esc(a.farmer_id)}</div></td><td>${a.match_score}%</td><td><b>${a.recommended_quantity} qtl</b></td><td>${esc(a.centre)}</td><td>${app?.status==='Approved'||app?.status==='Scheduled'?'<span class="pill success">Approved ✓</span>':'<span class="pill warning">Pending</span>'}</td></tr>`}).join('')}</tbody></table>`:'<div class="empty">Allocation will appear after ML matching.</div>'}</div>`}
function schedulingPage(){const scheduled=state.farmers.filter(f=>f.status==='Scheduled'||f.status==='Approved');return `<div class="page-head"><div><h2>Automated Scheduling</h2><div class="muted">Approval automatically assigns the next available centre slot and procurement token.</div></div><button class="btn outline" onclick="refreshState()">↻ Refresh</button></div><div class="card"><table><thead><tr><th>Farmer</th><th>Approved qty</th><th>Centre</th><th>Slot</th><th>Token</th><th>Status</th></tr></thead><tbody>${scheduled.length?scheduled.map(f=>{const a=state.applications.find(x=>x.application_id===f.applicationId);return `<tr><td><b>${esc(f.name)}</b><div class="muted">${esc(f.id)}</div></td><td>${a?.allocation||f.approvedQty||0} qtl</td><td>${esc(a?.centre||state.requirement.centre)}</td><td>${a?.schedule_date?esc(a.schedule_date)+' · '+esc(a.schedule_slot):'Pending automatic slot'}</td><td>${esc(a?.token||'—')}</td><td>${a?.status==='Scheduled'?`<button class="btn secondary" onclick="completeProcurement('${esc(a.application_id)}')">Mark completed</button>`:'<span class="pill warning">Awaiting scheduling</span>'}</td></tr>`}).join(''):'<tr><td colspan="6" class="empty">Approve a farmer recommendation. Scheduling happens automatically at approval.</td></tr>'}</tbody></table></div>`}
function capacityPage(){const r=state.requirement,used=r.allocated||0,available=Math.max(0,(r.capacity||0)-used);return `<div class="page-head"><div><h2>Centre Capacity</h2><div class="muted">Capacity is calculated from approved/scheduled database allocations.</div></div></div><div class="grid three"><div class="stat"><div class="label">Centre</div><div class="value" style="font-size:18px">${esc(r.centre)}</div></div><div class="stat"><div class="label">Used capacity</div><div class="value">${used} qtl</div></div><div class="stat"><div class="label">Available capacity</div><div class="value">${available} qtl</div></div></div><div class="card" style="margin-top:16px"><div class="section-title">Capacity utilisation</div><div class="progress"><i style="width:${pct(used,r.capacity)}%"></i></div><div style="display:flex;justify-content:space-between;margin-top:9px"><span>${used} qtl used</span><span>${r.capacity||0} qtl total</span></div></div>`}
function render(){layout();if(state.loading){document.getElementById('appContent').innerHTML='<div class="card"><b>Loading AnnSeva data…</b><p class="muted">Connecting to the SQLite-backed server.</p></div>';return}const pages={dashboard:farmerDashboard,submit:submitPage,applications:applicationsPage,schedule:schedulePage,notifications:notificationsPage,adminDashboard,requirements:requirementsPage,farmers:farmersPage,recommendations:recommendationsPage,scheduling:schedulingPage,capacity:capacityPage};document.getElementById('appContent').innerHTML=(pages[state.page]||farmerDashboard)();}
async function submitCrop(e){
 e.preventDefault();
 const form=e.currentTarget;
 const crop=document.getElementById('crop').value;
 const qty=Number(document.getElementById('qty').value);
 const loc=document.getElementById('loc').value.trim();
 const lat=document.getElementById('lat').value;
 const lon=document.getElementById('lon').value;
 if(!qty||qty<=0||!loc){toast('Enter valid crop, quantity and location');return}
 const btn=form.querySelector('button[type="submit"],button:last-child');
 if(btn){btn.disabled=true;btn.textContent='Submitting…'}
 try{
  await api('/api/applications',{method:'POST',body:JSON.stringify({farmer_id:state.farmer.id,name:state.farmer.name,crop,quantity:qty,location:loc,latitude:lat?Number(lat):null,longitude:lon?Number(lon):null})});
  state.page='applications';
  await refreshState(false);
  toast('Crop availability saved successfully.');
 }catch(err){toast(err.message)}
 finally{if(btn){btn.disabled=false;btn.textContent='Submit Procurement Application'}}
}
async function createRequirement(e){e.preventDefault();const crop=document.getElementById('reqCrop').value,needed=Number(document.getElementById('reqQty').value),centre=document.getElementById('reqCentre').value.trim(),capacity=Number(document.getElementById('reqCap').value),slotCapacity=Number(document.getElementById('reqSlotCap').value),lat=document.getElementById('reqLat').value,lon=document.getElementById('reqLon').value;if(!needed||needed<=0||!centre||!capacity||capacity<=0||!slotCapacity||slotCapacity<=0){toast('Enter valid requirement values');return}try{await api('/api/requirements',{method:'POST',body:JSON.stringify({crop,required_quantity:needed,centre,centre_capacity:capacity,slot_capacity:slotCapacity,latitude:lat?Number(lat):null,longitude:lon?Number(lon):null})});state.mlResults=null;state.page='recommendations';await refreshState(true);await runMLMatching();toast('Requirement saved and existing standing offers matched automatically.')}catch(err){toast(err.message)}}
async function runMLMatching(){const status=document.getElementById('mlStatus');if(status)status.innerHTML='<b>Running ML recommendation engine…</b><div class="muted">Sending live database applications to FastAPI.</div>';try{state.mlResults=await api('/api/recommendations',{method:'POST',body:JSON.stringify({top_k:10})});render();toast(state.mlResults.model==='random_forest'?'Random Forest recommendations ready':'Cold-start baseline recommendations ready')}catch(err){if(status)status.innerHTML=`<b>ML backend error</b><div class="muted">${esc(err.message)}</div>`;toast(err.message)}}
async function approve(applicationId){try{await api('/api/approve',{method:'POST',body:JSON.stringify({application_id:applicationId})});await refreshState(true);state.mlResults=null;state.page='recommendations';await runMLMatching();toast('Application approved, allocated and automatically scheduled')}catch(err){toast(err.message)}}
async function completeProcurement(applicationId){const actual=prompt('Actual quantity received (qtl):');if(actual===null)return;const qty=Number(actual);if(!qty||qty<=0){toast('Enter a valid quantity');return}try{await api('/api/complete',{method:'POST',body:JSON.stringify({application_id:applicationId,actual_quantity:qty})});await refreshState(true);toast('Procurement marked completed; ML outcome recorded')}catch(err){toast(err.message)}}
async function verifyFarmer(farmerId){try{await api(`/api/farmers/${encodeURIComponent(farmerId)}/verify`,{method:"POST"});await refreshState(true);toast("Farmer verified") }catch(err){toast(err.message)}}
async function scheduleFarmer(applicationId){try{await api('/api/schedule',{method:'POST',body:JSON.stringify({application_id:applicationId})});await refreshState(true);toast('Farmer scheduled and notification created');state.page='scheduling';render()}catch(err){toast(err.message)}}
async function resetDemo(){if(!confirm('Reset AnnSeva database and restore demo data?'))return;try{await api('/api/reset',{method:'POST'});state.mlResults=null;state.page=state.role==='admin'?'adminDashboard':'dashboard';await refreshState(true);toast('Demo database reset')}catch(err){toast(err.message)}}
function openLogin(role){document.getElementById('loginRole').value=role;toggleFarmerId();document.getElementById('loginPassword').value='';document.getElementById('loginOverlay').style.display='flex';document.getElementById('loginPassword').focus()}
document.getElementById('farmerBtn').onclick=()=>openLogin('farmer');
document.getElementById('adminBtn').onclick=()=>openLogin('admin');
window.addEventListener('load',()=>{toggleFarmerId();if(state.token){document.getElementById('loginOverlay').style.display='none';refreshState(true)}});

// Cross-tab / multi-window synchronization: every tab reads the same SQLite source of truth.
setInterval(()=>{
  if(document.hidden) return;
  // Do not even start a background refresh while a farmer/admin is editing a form.
  // This prevents a concurrent response from replacing the form underneath the user.
  const activeElement=document.activeElement;
  const editingForm=!!activeElement && !!activeElement.closest && !!activeElement.closest('form');
  if(['submit','requirements'].includes(state.page) || editingForm) return;
  refreshState(true);
},3000);
refreshState();

function toggleChat(){document.getElementById('chatPanel').classList.toggle('open');if(document.getElementById('chatPanel').classList.contains('open'))document.getElementById('chatInput').focus()}
function addChatMessage(text,who){const box=document.getElementById('chatMessages');const el=document.createElement('div');el.className='chat-msg '+who;el.textContent=text;box.appendChild(el);box.scrollTop=box.scrollHeight}
function quickChat(text){document.getElementById('chatInput').value=text;sendChat({preventDefault(){}})}
async function sendChat(e){e.preventDefault();const input=document.getElementById('chatInput');const text=input.value.trim();if(!text)return;if(!state.token){addChatMessage('Please sign in first so I can securely access your AnnSeva information.','bot');return}addChatMessage(text,'user');input.value='';try{const data=await api('/api/chat',{method:'POST',body:JSON.stringify({message:text})});addChatMessage(data.reply,'bot')}catch(err){addChatMessage('Sorry, I could not reach the AnnSeva assistant. Please try again.','bot')}}

/* vault-memory panel — searches the vault from a local page, no backend and no network.
   The index travels embedded in DATA (built by panel.py). */
const E = s => String(s == null ? "—" : s).replace(/[&<>"']/g, c =>
  ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
const nrm = s => (s == null ? "" : String(s)).toLowerCase()
  .normalize("NFD").replace(/[̀-ͯ]/g, "");
const ICON = {incident:"🔴",runbook:"📘",project:"📦",meeting:"🗣",decision:"⚖",
              period:"📅",daily:"📓",reference:"🗄",doc:"📄",home:"🏠",
              template:"🧩",note:"📝"};
const pill = (t,v) => '<span class="pill'+(v?" pill--"+v:"")+'">'+E(t)+'</span>';
const obs = p => "obsidian://open?vault=" + encodeURIComponent(DATA.vault)
                 + "&file=" + encodeURIComponent(p);

function search(q, n){
  const nq = nrm(q).trim();
  if(nq.length < 2) return [];
  const toks = nq.split(/[^a-z0-9\-\._]+/).filter(t => t.length >= 2);
  const exact = toks.length === 1 && /^(\d{3,10}|[a-z]{2,6}-\d{1,6})$/.test(nq);
  const out = [];
  for(const d of DATA.notes){
    let s = 0;
    const w = " " + d.w + " ", tt = nrm(d.t + " " + d.n);
    if(exact){
      if((d.ids||[]).includes(nq)) s += 40;
      if((d.k||[]).some(x => x.toLowerCase() === nq)) s += 40;
      if(tt.includes(nq)) s += 12;
    } else {
      for(const t of toks){
        if(w.includes(" "+t+" "))                 s += 6;
        else if(t.length >= 5 && w.includes(" "+t)) s += 2;
        // d.w carries stems (core.py stemmer): "cancelled" matches "cancel".
        else if(d.w.split(" ").some(x => x.length >= 4 && t.startsWith(x))) s += 4;
        if(tt.includes(t))                        s += 5;
        if((d.ids||[]).includes(t))               s += 40;
        if((d.k||[]).some(x => x.toLowerCase() === t)) s += 40;
      }
      if(tt.includes(nq)) s += 8;
      // tie-breaker, not a criterion: without a match, "being open" cannot add on its own
      if(s > 0 && d.e && !DATA.closed.includes(nrm(d.e))) s += 1;
    }
    if(s > 0) out.push({s, d});
  }
  out.sort((a,b) => b.s - a.s);
  return out.slice(0, n || 12);
}

function resultRow(x){
  const d = x.d;
  const meta = [d.tp, d.a, d.f || ("touched "+d.m), d.e, d.sv, d.sys]
    .filter(Boolean).map(v => pill(v, v==="P1"?"crit":"")).join("");
  const rel = (DATA.rel[d.p] || []).slice(0,3);
  return '<div class="res"><div class="meta">'+pill(ICON[d.tp]||"·")+meta
    + (d.bl ? '<span class="pill mono">'+d.bl+' backlinks</span>' : "") + '</div>'
    + '<p class="ti"><a href="'+E(obs(d.p))+'">'+E(d.t)+'</a></p>'
    + '<p class="dim">'+E(d.s||"—")+'</p>'
    + '<p class="mono dim">'+E(d.p)+'</p>'
    + (rel.length ? '<p class="dim" style="font-size:13px">Similar: '
        + rel.map(r => '<a href="'+E(obs(r.p))+'">'+(ICON[r.type]||"·")+" "+E(r.t)+'</a>')
             .join(" · ") + '</p>' : "")
    + '</div>';
}

function freshness(){
  const ms = Date.now() - Date.parse(DATA.generated_iso);
  const h = ms/3600000, min = Math.round(ms/60000);
  const otherDay = DATA.generated_iso.slice(0,10) !== new Date().toISOString().slice(0,10);
  return { txt: min<1 ? "just generated" : min<60 ? "generated "+min+" min ago"
              : h<24 ? "generated "+Math.round(h)+" h ago"
              : "generated "+Math.round(h/24)+" d ago",
           level: (otherDay||h>=12) ? "crit" : h>=3 ? "warn" : "ok",
           old: otherDay||h>=3, date: DATA.generated_iso.slice(0,10) };
}

function tile(n,l,d,v){ return '<div class="tile tile--'+v+'"><b>'+E(n)+'</b>'
  +'<span>'+E(l)+'</span><em>'+E(d)+'</em></div>'; }

function card(t,c,body){ return '<section class="card"><h2>'+E(t)
  +'<span class="c">'+E(c)+'</span></h2>'+body+'</section>'; }

const MOD = n => (DATA.mods || {})[n] !== false;

function render(){
  const S = DATA.health, f = freshness();
  let H = '<header><h1>🧠 '+E(DATA.vault)+'</h1>'
    + '<span class="sub">'+E(S.notes)+' indexed notes</span><span class="sp"></span>'
    + pill(f.txt, f.level) + '</header>';

  if(f.old) H += '<div class="banner"><b>This panel is a snapshot from '+E(f.date)+'</b> — '
    + E(f.txt) + '. Regenerate it with <span class="mono">vault-memory panel</span>.</div>';

  const tiles = [tile(S.notes, "Notes", S.edges+" links between them", "acc")];
  if(MOD("contract") && S.contract_coverage != null)
    tiles.push(tile(S.contract_coverage+"%", "Contract",
      S.conforming+" with complete frontmatter", S.contract_coverage>=95?"ok":"warn"));
  if(MOD("patterns"))
    tiles.push(tile(S.open_records, "Open records",
      "of "+S.records+" record notes", S.open_records?"warn":"ok"));
  if(MOD("graph"))
    tiles.push(tile(S.broken_links, "Broken links", S.orphans+" unlinked notes",
      S.broken_links?"warn":"ok"));
  H += '<div class="tiles">' + tiles.join("") + '</div>';

  H += card("Ask the vault", DATA.notes.length+" notes · no network, no tokens",
      '<div class="pad"><input id="q" type="search" autofocus autocomplete="off" '
      + 'placeholder="free text · an ID · a ticket key…"><p class="dim" '
      + 'style="margin:8px 0 0;font-size:13px">Searches by text, by ID and by ticket. '
      + 'If nothing shows up here, only then is it worth searching elsewhere.</p></div>'
      + '<div id="res"></div>');

  const rec = MOD("patterns") ? DATA.recurrence.filter(r => r.n >= 2) : [];
  if(rec.length) H += card("What repeats", rec.length+" classes with more than one occurrence",
      '<div class="scroll"><table><thead><tr><th>Class</th><th class="num">Times</th>'
      + '<th class="num">Open</th><th>Last</th>'
      + (DATA.has_taxonomy ? '<th>Written solution</th>' : '')
      + '<th>Systems</th></tr></thead><tbody>'
      + rec.map(r => '<tr><td class="mono">'+E(r.class)+'</td><td class="num">'+E(r.n)+'</td>'
          + '<td class="num">'+E(r.open)+'</td><td class="dim">'+E(r.last||"—")+'</td>'
          + (DATA.has_taxonomy
              ? '<td>'+pill(r.documented?"yes":"no", r.documented?"ok":"crit")+'</td>' : '')
          + '<td class="dim">'+E((r.systems||[]).join(" · "))+'</td></tr>').join("")
      + '</tbody></table></div>');

  if(MOD("patterns") && DATA.stale.length) H += card("Open for a while",
      DATA.stale.length+" not closed",
      '<div>'+DATA.stale.map(e => '<div class="res"><div class="meta">'
        + pill(e.days+" d", e.days>90?"crit":"warn") + (e.sev?pill(e.sev):"")
        + pill(e.status) + '</div><p class="ti"><a href="'+E(obs(e.p))+'">'
        + E(e.t)+'</a></p></div>').join("")+'</div>');

  const H2 = [];
  const push = (t, items) => { if(items && items.length) H2.push('<h4>'+t+'</h4><ul>'+items+'</ul>'); };
  if(DATA.debt.length) push("Repeats and has no written solution",
    DATA.debt.map(r => '<li><span class="mono">'+E(r.class)+'</span> — '+r.n
      +' times, last '+E(r.last||"—")+'</li>').join(""));
  if(DATA.outside.length) push("Outside the taxonomy",
    DATA.outside.map(r => '<li><span class="mono">'+E(r.class)+'</span> — '+r.n+'</li>').join(""));
  push("Unwritten runbooks", DATA.pending_runbooks.map(p =>
    '<li><a href="'+E(obs(p))+'">'+E(p.split("/").pop().replace(/\.md$/,""))+'</a></li>').join(""));
  if(MOD("graph")){
  push("Notes nobody links", DATA.orphans.slice(0,12).map(p =>
    '<li><a href="'+E(obs(p))+'">'+E(p.split("/").pop().replace(/\.md$/,""))+'</a></li>').join(""));
  push("Broken wikilinks", DATA.broken.slice(0,12).map(r =>
    '<li><span class="mono">'+E(r.link)+'</span> <span class="dim">from '
      +E(r.from.split("/").pop())+'</span></li>').join(""));
  push("Duplicate note names (wikilinks will fail)",
    Object.entries(DATA.duplicates).slice(0,8).map(([n,ps]) =>
      '<li><span class="mono">'+E(n)+'</span> — '+ps.length+' notes</li>').join(""));
  }
  if(H2.length) H += card("Memory gaps", "what cannot be crossed yet",
    '<div class="pad">'+H2.join("")+'</div>');

  H += '<footer>Generated by <span class="mono">vault-memory panel</span> on '
     + E(DATA.generated) + '. Everything is computed from the vault: the page calls no network.'
     + ' Links open the note in Obsidian.</footer>';

  document.getElementById("app").innerHTML = H;

  const q = document.getElementById("q"), r = document.getElementById("res");
  let t = null;
  q.oninput = () => { clearTimeout(t); t = setTimeout(() => {
    const v = q.value;
    r.innerHTML = v.trim().length < 2 ? ""
      : (search(v,12).map(resultRow).join("") ||
         '<div class="res"><p class="empty">Nothing in the vault for “'+E(v)+'”.</p></div>');
  }, 110); };
  q.onkeydown = e => { if(e.key === "Escape"){ q.value=""; q.oninput(); } };
  document.addEventListener("keydown", e => {
    if(e.key === "/" && !e.target.matches("input")){ e.preventDefault(); q.focus(); }
  });
}
render();

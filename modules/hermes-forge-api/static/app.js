(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  let session = "";
  let current = null;
  let catalog = null;

  const $ = (id) => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = value == null ? "—" : String(value); };
  const result = (id, value, bad=false) => { const el=$(id); el.textContent=value||""; el.style.color=bad?"#9b2e22":"#555"; };
  const pill = (el, label, kind="") => { el.textContent=label; el.className="pill"+(kind?" "+kind:""); };

  async function request(path, options={}) {
    const headers = {"Content-Type":"application/json", ...(options.headers||{})};
    if (session) headers.Authorization = "Bearer " + session;
    const response = await fetch(path, {...options, headers});
    const payload = await response.json().catch(() => ({ok:false,error:"invalid_response"}));
    if (!response.ok || payload.ok !== true) throw new Error(payload.error || "request_failed");
    return payload.result;
  }

  async function authenticate() {
    if (!tg || !tg.initData) throw new Error("Открой Hermes Forge внутри Telegram");
    tg.ready(); tg.expand();
    const auth = await request("/v1/auth/telegram", {method:"POST", body:JSON.stringify({init_data:tg.initData})});
    session = auth.session;
    text("auth-state", "Безопасная сессия подключена");
    const [catalogData, hermesData] = await Promise.all([request("/v1/catalog"), request("/v1/hermes")]);
    catalog = catalogData;
    renderCatalog(catalog);
    $("app").classList.remove("hidden");
    const items = hermesData.items || [];
    if (!items.length) {
      $("empty-state").classList.remove("hidden");
      document.querySelectorAll("[data-requires-hermes]").forEach(x=>x.classList.add("hidden"));
      selectTab("employees");
      return;
    }
    current = items[0];
    renderHermes(current);
    await Promise.all([loadConnections(), loadSecrets()]);
  }

  function renderHermes(item) {
    text("bot-name", item.bot_name || "Hermes");
    text("bot-username", item.bot_username ? "@"+item.bot_username : item.profile);
    const h=item.health||{};
    pill($("status-pill"), h.healthy ? "Онлайн" : "Требует внимания", h.healthy?"ok":"bad");
    text("runtime-version", h.release_id || h.code_version);
    text("telegram-state", h.telegram);
    text("telemetry-state", h.telemetry_enabled ? "Включена" : "Выключена");
    text("active-agents", h.active_agents ?? 0);
  }

  function selectTab(name) {
    document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.tab===name));
    document.querySelectorAll("[data-panel]").forEach(x=>x.classList.toggle("hidden",x.dataset.panel!==name));
  }

  function catalogCard(item, statusLabel, statusKind) {
    const card=document.createElement("div"); card.className="card catalog-card";
    const top=document.createElement("div"); top.className="catalog-top";
    const titleWrap=document.createElement("div");
    const title=document.createElement("h2"); title.textContent=item.name||item.id;
    const publisher=document.createElement("div"); publisher.className="muted"; publisher.textContent=item.publisher||"Hermes";
    titleWrap.append(title,publisher);
    const state=document.createElement("span"); pill(state,statusLabel,statusKind);
    top.append(titleWrap,state);
    const summary=document.createElement("p"); summary.className="catalog-summary"; summary.textContent=item.summary||"";
    const meta=document.createElement("div"); meta.className="catalog-meta";
    for (const value of [item.kind||item.role, item.version ? "v"+item.version : "", item.connection_mode||""]) {
      if (!value) continue; const span=document.createElement("span"); span.textContent=value; meta.append(span);
    }
    card.append(top,summary,meta);
    return card;
  }

  function renderCatalog(data) {
    const capabilities=(data&&data.capabilities)||[];
    const agents=(data&&data.agents)||[];
    text("tools-count", capabilities.length);
    text("employees-count", agents.length);
    const toolsRoot=$("tools-list"); toolsRoot.textContent="";
    const sorted=[...capabilities].sort((a,b)=>Number(b.availability==="available")-Number(a.availability==="available") || String(a.name).localeCompare(String(b.name)));
    for (const item of sorted) {
      const available=item.availability==="available";
      const card=catalogCard(item,available?"Доступен":"Скоро",available?"ok":"warn");
      const meta=document.createElement("div"); meta.className="catalog-capabilities";
      const action=document.createElement("span"); action.className="chip"; action.textContent="Режим: "+(item.action_default||"observe"); meta.append(action);
      if (item.metering && item.metering!=="none") { const metering=document.createElement("span"); metering.className="chip"; metering.textContent="Usage metering"; meta.append(metering); }
      card.append(meta); toolsRoot.append(card);
    }
    const employeesRoot=$("employees-list"); employeesRoot.textContent="";
    const nameById=new Map(capabilities.map(x=>[x.id,x.name||x.id]));
    for (const item of agents) {
      const card=catalogCard(item,"Official","ok");
      const chips=document.createElement("div"); chips.className="catalog-capabilities";
      for (const id of item.required_capabilities||[]) { const chip=document.createElement("span"); chip.className="chip"; chip.textContent="✓ "+(nameById.get(id)||id); chips.append(chip); }
      for (const id of item.optional_capabilities||[]) { const chip=document.createElement("span"); chip.className="chip"; chip.textContent="+ "+(nameById.get(id)||id); chips.append(chip); }
      card.append(chips); employeesRoot.append(card);
    }
  }

  async function loadConnections() {
    if (!current) return;
    const data = await request(`/v1/hermes/${current.profile}/connections`);
    const root=$("connections-list"); root.textContent="";
    for (const item of data.items||[]) {
      const card=document.createElement("div"); card.className="card connection";
      const left=document.createElement("div");
      const title=document.createElement("strong"); title.textContent=item.id==="maton"?"Maton":"Telegram";
      const sub=document.createElement("div"); sub.className="muted"; sub.textContent=item.id==="maton"?"MCP и внешние сервисы":"Канал связи с Hermes";
      left.append(title,sub);
      const state=document.createElement("span"); state.className="pill";
      const ok=item.id==="maton" ? item.configured : item.status==="connected";
      state.textContent=ok?"Подключён":"Не подключён"; state.classList.add(ok?"ok":"warn");
      card.append(left,state); root.append(card);
    }
  }

  async function loadSecrets() {
    if (!current) return;
    const data=await request(`/v1/hermes/${current.profile}/secrets`);
    const item=(data.items||[]).find(x=>x.name==="MCP_MATON_API_KEY");
    if (item && item.configured) pill($("maton-secret-state"), `••••${item.last4}`, "ok");
    else pill($("maton-secret-state"), "Не подключён", "warn");
  }

  async function refreshHermes() {
    current=await request(`/v1/hermes/${current.profile}`); renderHermes(current);
  }

  document.addEventListener("click", async (event) => {
    const tab=event.target.closest(".tab");
    if (tab) { selectTab(tab.dataset.tab); return; }
    if (!current) return;
    const button=event.target.closest("button"); if (!button) return;
    button.disabled=true;
    try {
      if (button.id==="health-button") {
        result("hermes-result","Проверяю…");
        const h=await request(`/v1/hermes/${current.profile}/health-check`,{method:"POST",body:"{}"});
        result("hermes-result",h.healthy?"Все базовые проверки пройдены":"Есть компонент, требующий внимания",!h.healthy); await refreshHermes();
      } else if (button.id==="restart-button") {
        result("hermes-result","Перезапускаю только idle Hermes…");
        await request(`/v1/hermes/${current.profile}/restart`,{method:"POST",body:"{}"}); result("hermes-result","Hermes снова онлайн"); await refreshHermes();
      } else if (button.id==="save-maton") {
        const value=$("maton-key").value.trim(); if(!value) throw new Error("Вставь Maton API key");
        result("secret-result","Проверяю ключ Maton…");
        await request(`/v1/hermes/${current.profile}/secrets/MCP_MATON_API_KEY`,{method:"PUT",body:JSON.stringify({value})});
        $("maton-key").value=""; result("secret-result","Ключ проверен и сохранён. Перезапусти Hermes для применения."); await Promise.all([loadSecrets(),loadConnections()]);
      } else if (button.id==="test-maton") {
        result("secret-result","Проверяю Maton…");
        const data=await request(`/v1/hermes/${current.profile}/connections/maton/test`,{method:"POST",body:"{}"});
        result("secret-result",data.check==="valid"?"Maton отвечает, ключ валиден":`Статус Maton: ${data.check}`,data.check!=="valid");
      } else if (button.id==="delete-maton") {
        await request(`/v1/hermes/${current.profile}/secrets/MCP_MATON_API_KEY`,{method:"DELETE",body:"{}"});
        result("secret-result","Ключ удалён. Перезапусти Hermes для применения."); await Promise.all([loadSecrets(),loadConnections()]);
      }
    } catch (err) {
      const message=String(err && err.message || err); const target=button.id.includes("maton")?"secret-result":"hermes-result"; result(target,message,true);
    } finally { button.disabled=false; }
  });

  authenticate().catch(err => { text("auth-state", String(err && err.message || err)); $("empty-state").classList.remove("hidden"); });
})();

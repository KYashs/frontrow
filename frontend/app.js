// FrontRow frontend. Static files come from S3 through CloudFront; everything
// under /api goes CloudFront -> WAF -> ALB -> API pod on EKS; live seat changes arrive
// over an API Gateway WebSocket fed by DynamoDB Streams.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const PALETTE = ["#ff3d7f", "#ff6a3d", "#ff9a1f", "#ffc93c", "#c6e85a", "#5fdc8a", "#2fd1c5", "#3ea8ff", "#6f7cff", "#a77cff"];
  const MAX = 8;
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const S = {
    cfg: {}, events: [], ev: null, venue: null, info: {}, colors: {}, avail: {}, map: null,
    open: null, seatGrid: null, picks: new Map(), ga: null, hold: null, ws: null, wsRetry: 0,
    timers: [], queuePoll: null,
  };

  // ------------------------------------------------------------ AWS path trace
  const trace = {
    items: [],
    add(entry) {
      this.items.unshift(entry);
      this.items = this.items.slice(0, 40);
      if (!$("trace").hidden) this.render();
    },
    hops(e) {
      const h = [];
      const why = [];
      if (e.ws) {
        return { h: [["DynamoDB Streams", ""], ["Lambda stream processor", ""], ["API Gateway WebSocket", "ws"]], why: e.why };
      }
      if (e.pop) h.push([`CloudFront ${e.pop}`, "edge"], ["AWS WAF", ""]);
      else h.push(["Local origin", ""]);
      const hit = (e.cache || "").toLowerCase().startsWith("hit");
      if (hit) {
        h.push(["Edge cache hit", "hit"]);
        why.push("Answered at the edge. The origin never saw this request.");
        return { h, why: why.join(" ") };
      }
      h.push(["ALB", ""], [`EKS pod ${e.served || ""}`.trim(), ""]);
      const p = e.path;
      if (/^\/events(\/[^/]+)?$/.test(p)) { h.push(["RDS PostgreSQL", ""]); why.push("Relational catalogue: events, venues, price tiers. Cached at the edge for 30s."); }
      else if (p.includes("/availability")) { h.push(["DynamoDB", ""]); why.push("Section counters kept current by the stream processor. Edge-cached for 2s."); }
      else if (p.includes("/seats")) { h.push(["DynamoDB", ""]); why.push("One partition per section, so a single Query returns the whole seat grid."); }
      else if (p.includes("/join")) { h.push(["DynamoDB sharded counter", ""]); why.push("Your place in line comes from one of 10 counters, so joins never pile onto one hot item."); }
      else if (p.includes("/status")) { why.push("Signed ticket checked with HMAC on the server. No database write, so polling is cheap."); }
      else if (p.startsWith("/holds") && e.method === "POST") { h.push(["DynamoDB transaction", ""]); why.push("Seats are held only if every one is still free, in one atomic write. This is what stops double-selling."); }
      else if (p.startsWith("/holds") && e.method === "DELETE") { h.push(["DynamoDB", ""]); why.push("Hold released and inventory returned in one transaction."); }
      else if (p === "/orders" && e.method === "POST") { h.push(["DynamoDB", ""], ["SQS FIFO", ""], ["Lambda checkout", ""], ["EventBridge", ""], ["SES + Firehose", ""]); why.push("The API answers in milliseconds. Payment and confirmation run asynchronously off the queue."); }
      else if (p.startsWith("/orders")) { h.push(["DynamoDB", ""]); why.push("Polling the order the checkout worker wrote."); }
      return { h, why: why.join(" ") };
    },
    render() {
      const list = $("trace-list");
      if (!this.items.length) { list.innerHTML = '<li class="trace-empty">Use the page and requests will appear here.</li>'; return; }
      list.innerHTML = this.items.map((e) => {
        const { h, why } = this.hops(e);
        const head = e.ws ? `<code>WebSocket ${esc(e.label)}</code><em>live</em>`
          : `<code>${esc(e.method)} /api${esc(e.path)}</code><em>${e.status} in ${Math.round(e.ms)} ms</em>`;
        return `<li><div class="req">${head}</div><div class="hops">${h.map(([t, c]) => `<span class="hop ${c}">${esc(t)}</span>`).join("")}</div>${why ? `<p class="why">${esc(why)}</p>` : ""}</li>`;
      }).join("");
    },
  };
  $("trace-toggle").addEventListener("click", () => {
    const open = $("trace").hidden;
    $("trace").hidden = !open;
    $("trace-toggle").setAttribute("aria-expanded", String(open));
    if (open) trace.render();
  });
  $("trace-close").addEventListener("click", () => { $("trace").hidden = true; $("trace-toggle").setAttribute("aria-expanded", "false"); });

  async function api(path, opts = {}) {
    const t0 = performance.now();
    let res;
    try {
      res = await fetch(`/api${path}`, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
    } catch (err) {
      throw Object.assign(new Error("network"), { status: 0, body: {} });
    }
    const h = (k) => res.headers.get(k);
    trace.add({ method: opts.method || "GET", path, status: res.status, ms: performance.now() - t0,
      pop: h("x-amz-cf-pop"), cache: h("x-cache"), served: h("x-served-by") });
    if (h("x-served-by")) $("served").textContent = `Last answered by API pod ${h("x-served-by")}.`;
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error("request failed"), { status: res.status, body });
    return body;
  }

  // ------------------------------------------------------------ formatting
  const money = (n, cur) => new Intl.NumberFormat(cur === "INR" ? "en-IN" : "en-US",
    { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(n);
  const when = (iso, tz) => new Date(iso).toLocaleString(undefined, { timeZone: tz, weekday: "short", day: "numeric",
    month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
  const seatLabel = (section, sk) => {
    const m = /^R(\d+)#S(\d+)$/.exec(sk);
    const label = S.info[section]?.label ?? section;
    return m ? `Section ${label}, row ${+m[1]}, seat ${+m[2]}` : `${label}`;
  };
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { t.hidden = true; }, 3200);
  }

  // ------------------------------------------------------------ phone layout
  const isPhone = () => matchMedia("(max-width: 640px)").matches;
  const scrollBehavior = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
  const revealOnPhone = (node) => { if (isPhone()) node.scrollIntoView({ behavior: scrollBehavior(), block: "start" }); };

  // ------------------------------------------------------------ routing
  function clearTimers() {
    S.timers.forEach(clearInterval);
    S.timers = [];
    clearInterval(S.queuePoll);
    if (S.ws) { S.ws.onclose = null; S.ws.close(); S.ws = null; }
  }
  function route() {
    clearTimers();
    const m = location.hash.match(/^#\/?show\/(.+)$/);
    if (m) showEvent(decodeURIComponent(m[1]));
    else showHome();
    window.scrollTo(0, 0);
  }

  // ------------------------------------------------------------ home (lineup poster)
  async function showHome() {
    $("event").hidden = true;
    $("home").hidden = false;
    document.title = "FrontRow";
    const list = $("lineup");
    if (!S.events.length) {
      list.innerHTML = '<li class="hint">Loading the lineup…</li>';
      try { S.events = (await api("/events")).events; } catch (e) {
        list.innerHTML = `<li class="msg err">The lineup didn't load (error ${e.status || "network"}). Check that the API pods are healthy, then refresh.</li>`;
        return;
      }
    }
    list.innerHTML = S.events.map((e) => `
      <li><a href="#show/${esc(e.event_id)}" style="--a1:${esc(e.art1)};--a2:${esc(e.art2)}">
        <span class="artist">${esc(e.artist)}</span>
        <span class="where">${esc(e.venue)}, ${esc(e.city)} <span>${esc(when(e.starts_at, e.tz))}</span></span>
        <span class="price"><b>From ${money(e.from_price, e.currency)}</b>${Number(e.capacity).toLocaleString()} tickets</span>
      </a></li>`).join("");
  }

  // ------------------------------------------------------------ event
  async function showEvent(id) {
    $("home").hidden = true;
    $("event").hidden = false;
    S.picks.clear(); S.ga = null; S.hold = null; S.open = null;
    $("panel-hold").hidden = true; $("panel-browse").hidden = false;
    $("picker").innerHTML = ""; $("hint").hidden = false;
    $("ev-artist").textContent = "Loading…"; $("ev-meta").textContent = "";
    try {
      const ev = await api(`/events/${encodeURIComponent(id)}`);
      const [venue, avail] = await Promise.all([
        fetch(`/venues/${ev.venue_id}.json`).then((r) => r.json()),
        api(`/events/${encodeURIComponent(id)}/availability`),
      ]);
      S.ev = ev; S.venue = venue; S.avail = avail.available; S._mode = avail.onsale.mode;
    } catch (e) {
      $("ev-artist").textContent = "Show not found";
      $("ev-meta").textContent = "Go back to the lineup and pick another show.";
      return;
    }
    const ev = S.ev;
    document.title = `${ev.artist} | FrontRow`;
    $("ev-artist").textContent = ev.artist;
    $("ev-meta").textContent = `${ev.venue}, ${ev.city}. ${when(ev.starts_at, ev.tz)}`;

    S.colors = {};
    ev.tiers.forEach((t, i) => { S.colors[t.tier] = PALETTE[i % PALETTE.length]; });
    const tierBy = Object.fromEntries(ev.tiers.map((t) => [t.tier, t]));
    S.info = {};
    for (const s of ev.sections) {
      const t = tierBy[s.tier];
      S.info[s.section_id] = { ...s, on_sale: !!s.on_sale, price: t?.price, tier_label: t?.name };
    }
    renderSectionSelect();
    S.map = SeatMap.render($("map"), S.venue, {
      sections: S.info, colors: S.colors, onClick: openSection, onHover: hover, onLeave: () => { $("tooltip").hidden = true; },
    });
    refreshAvailability(false);
    connectWs();
    S.timers.push(setInterval(() => refreshAvailability(true), 15000));

    const pass = store.get(`access:${ev.event_id}`);
    if (avail_mode() === "queue" && !(pass && pass.exp > Date.now() / 1000)) startQueue();
    else $("queue").hidden = true;
  }
  const avail_mode = () => S._mode;

  // Sections on a phone-sized map are smaller than a fingertip, so phones also
  // get a native list of every section.
  function renderSectionSelect() {
    const select = $("section-select");
    const ev = S.ev;
    select.innerHTML = '<option value="">Select a section</option>' + ev.tiers.map((t) => {
      const ids = ev.sections.filter((s) => s.tier === t.tier && s.on_sale).map((s) => s.section_id);
      if (!ids.length) return "";
      return `<optgroup label="${esc(t.name)}, ${esc(money(t.price, ev.currency))}">${ids.map((id) => `<option value="${esc(id)}">${esc(S.info[id].label)}</option>`).join("")}</optgroup>`;
    }).join("");
    select.onchange = (e) => { if (e.target.value) openSection(e.target.value); };
  }

  async function refreshAvailability(fetchIt) {
    if (fetchIt) {
      try {
        const a = await api(`/events/${encodeURIComponent(S.ev.event_id)}/availability`);
        S.avail = a.available; S._mode = a.onsale.mode;
      } catch { return; }
    }
    S.map.update(S.avail);
    renderTiers();
  }

  function renderTiers() {
    const agg = {};
    for (const [id, s] of Object.entries(S.info)) {
      if (!s.on_sale) continue;
      const a = (agg[s.tier] ||= { left: 0, cap: 0 });
      a.left += Math.max(0, S.avail[id] ?? 0);
      a.cap += s.capacity;
    }
    $("tiers").innerHTML = S.ev.tiers.map((t) => {
      const a = agg[t.tier] || { left: 0, cap: 1 };
      const pct = Math.round((100 * a.left) / a.cap);
      return `<li style="color:${S.colors[t.tier]}"><i style="background:${S.colors[t.tier]}"></i>
        <span style="color:var(--ink)">${esc(t.name)}</span><span class="amt" style="color:var(--ink)">${money(t.price, S.ev.currency)}</span>
        <span class="left" title="${a.left.toLocaleString()} left"><u style="width:${pct}%"></u></span></li>`;
    }).join("");
  }

  function hover(id, evt, node) {
    const s = S.info[id];
    const tip = $("tooltip");
    const left = Math.max(0, S.avail[id] ?? 0);
    tip.innerHTML = `<b>${s.kind === "seated" && /^\d/.test(s.label) ? "Section " : ""}${esc(s.label)}</b><br>${esc(s.tier_label)}, ${money(s.price, S.ev.currency)}<br>${left ? `${left.toLocaleString()} left` : "Sold out"}`;
    tip.hidden = false;
    const wrap = $("map").parentElement.getBoundingClientRect();
    const r = evt ? { x: evt.clientX, y: evt.clientY } : (() => { const b = node.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top }; })();
    const x = Math.min(r.x - wrap.left + 14, wrap.width - 170);
    tip.style.left = `${x}px`;
    tip.style.top = `${Math.max(8, r.y - wrap.top - 70)}px`;
  }

  // ------------------------------------------------------------ waiting room
  async function startQueue() {
    $("queue").hidden = false;
    const key = `ticket:${S.ev.event_id}`;
    let ticket = store.get(key);
    let first = null;
    const join = async () => {
      const j = await api(`/waiting-room/${encodeURIComponent(S.ev.event_id)}/join`, { method: "POST" });
      ticket = j.ticket; store.set(key, ticket);
      return j;
    };
    const apply = (st) => {
      if (st.state === "admitted") {
        store.set(`access:${S.ev.event_id}`, { token: st.access_token, exp: Date.now() / 1000 + 840 });
        clearInterval(S.queuePoll);
        $("queue").hidden = true;
        toast("You're in. Pick your seats.");
        return;
      }
      if (first === null) first = Math.max(st.ahead, 1);
      $("queue-ahead").textContent = st.ahead.toLocaleString();
      $("queue-ahead-label").textContent = st.ahead === 1 ? "person ahead of you" : "people ahead of you";
      $("queue-bar").style.width = `${Math.max(4, 100 - (100 * st.ahead) / first)}%`;
      const mins = Math.ceil(st.eta_seconds / 60);
      $("queue-eta").textContent = st.state === "before_open" ? "Sales haven't opened yet. You'll move as soon as they do."
        : st.eta_seconds < 60 ? "Less than a minute to go." : `About ${mins} minute${mins === 1 ? "" : "s"} to go.`;
    };
    const poll = async () => {
      try {
        let st = ticket ? await api(`/waiting-room/${encodeURIComponent(S.ev.event_id)}/status?ticket=${encodeURIComponent(ticket)}`) : await join();
        if (st.state === "invalid") st = await join();
        apply(st);
      } catch { /* keep the place in line; try again next tick */ }
    };
    await poll();
    if (!$("queue").hidden) S.queuePoll = setInterval(poll, 2000);
  }
  function accessToken() {
    const pass = store.get(`access:${S.ev.event_id}`);
    return pass && pass.exp > Date.now() / 1000 ? pass.token : null;
  }

  // ------------------------------------------------------------ picking
  async function openSection(id) {
    if (S.hold) return;
    const s = S.info[id];
    if (!s || !s.on_sale) return;
    S.open = id;
    S.map.setActive(id);
    $("section-select").value = id;
    $("hint").hidden = true;
    if (s.kind === "ga") {
      if (S.picks.size) { S.picks.clear(); toast("Switched to general admission. Seat picks cleared."); }
      S.ga = { section: id, qty: Math.min(2, Math.max(1, S.avail[id] ?? 1)) };
      renderPicker();
      revealOnPhone($("picker"));
      return;
    }
    S.ga = null;
    $("picker").innerHTML = `<div class="picker-head"><h3>Section ${esc(s.label)}</h3><span>Loading seats…</span></div>`;
    try {
      S.seatGrid = await api(`/events/${encodeURIComponent(S.ev.event_id)}/sections/${encodeURIComponent(id)}/seats`);
      S.seatGrid.status = Object.fromEntries(S.seatGrid.seats);
    } catch {
      $("picker").innerHTML = '<p class="msg err">Seats for this section didn\'t load. Tap the section to try again.</p>';
      return;
    }
    renderPicker();
    revealOnPhone($("picker"));
  }

  function renderPicker() {
    const box = $("picker");
    const cur = S.ev.currency;
    const oldGrid = $("seat-grid");
    const keepScroll = oldGrid && oldGrid.dataset.section === S.open ? [oldGrid.scrollLeft, oldGrid.scrollTop] : null;
    if (S.ga) {
      const s = S.info[S.ga.section];
      const left = Math.max(0, S.avail[S.ga.section] ?? 0);
      box.innerHTML = `
        <div class="picker-head"><h3>${esc(s.label)}</h3><span>Standing, ${money(s.price, cur)} each</span></div>
        ${left ? `<div class="qty"><button type="button" id="q-minus" aria-label="One fewer ticket">−</button>
          <output id="q-out" aria-live="polite">${S.ga.qty}</output>
          <button type="button" id="q-plus" aria-label="One more ticket">+</button></div>` : '<p class="msg err">Sold out. Try another area.</p>'}
        ${summary()}`;
      if (left) {
        $("q-minus").onclick = () => { S.ga.qty = Math.max(1, S.ga.qty - 1); renderPicker(); };
        $("q-plus").onclick = () => { S.ga.qty = Math.min(MAX, left, S.ga.qty + 1); renderPicker(); };
      }
    } else if (S.open && S.seatGrid) {
      const s = S.info[S.open];
      const color = S.colors[s.tier];
      const rows = [];
      for (let r = 1; r <= S.seatGrid.rows; r++) {
        const cells = [];
        for (let n = 1; n <= S.seatGrid.seats_per_row; n++) {
          const sk = `R${String(r).padStart(2, "0")}#S${String(n).padStart(2, "0")}`;
          const st = S.seatGrid.status[sk] || "s";
          const picked = S.picks.has(`${S.open}|${sk}`);
          cells.push(`<button type="button" class="seat ${picked ? "pick" : st}" data-sk="${sk}" ${st !== "a" && !picked ? "disabled" : ""}
            aria-pressed="${picked}" aria-label="Row ${r} seat ${n}${st !== "a" && !picked ? ", taken" : ""}"></button>`);
        }
        rows.push(`<div class="seat-row"><b>${r}</b>${cells.join("")}</div>`);
      }
      box.innerHTML = `
        <div class="picker-head"><h3>Section ${esc(s.label)}</h3><span>${money(s.price, cur)} each</span></div>
        <div class="seats" id="seat-grid" data-section="${esc(S.open)}" style="--tier:${color}">${rows.join("")}</div>
        <p class="screen-note">Row 1 is closest to the field. Pick up to ${MAX} seats.</p>
        ${summary()}`;
      // Each pick redraws the grid; keep the buyer's place in it.
      if (keepScroll) [$("seat-grid").scrollLeft, $("seat-grid").scrollTop] = keepScroll;
      $("seat-grid").onclick = (e) => {
        const b = e.target.closest(".seat");
        if (!b || b.disabled) return;
        const k = `${S.open}|${b.dataset.sk}`;
        const adding = !S.picks.has(k);
        if (S.picks.has(k)) S.picks.delete(k);
        else if (S.picks.size < MAX) S.picks.set(k, { section: S.open, seat: b.dataset.sk });
        else { toast(`You can pick up to ${MAX} seats.`); return; }
        renderPicker();
        const seat = $("seat-grid").querySelector(`[data-sk="${b.dataset.sk}"]`);
        if (adding) seat.classList.add("pop");
        seat.focus({ preventScroll: true });
      };
    } else {
      box.innerHTML = summary();
    }
    const btn = $("hold-btn");
    if (btn) btn.onclick = createHold;
  }

  function summary() {
    const cur = S.ev.currency;
    let lines = [];
    if (S.ga) {
      const s = S.info[S.ga.section];
      lines = [[`${S.ga.qty} × ${s.label}`, S.ga.qty * s.price]];
    } else {
      lines = [...S.picks.values()].map((p) => [seatLabel(p.section, p.seat), S.info[p.section].price]);
    }
    if (!lines.length) return "";
    const total = lines.reduce((a, [, v]) => a + v, 0);
    const n = S.ga ? S.ga.qty : S.picks.size;
    return `<ul class="summary">${lines.map(([l, v]) => `<li><span>${esc(l)}</span><span>${money(v, cur)}</span></li>`).join("")}
      <li class="tot"><span>Total</span><span>${money(total, cur)}</span></li></ul>
      <button type="button" class="btn" id="hold-btn">Hold ${n} ${n === 1 ? "ticket" : "tickets"} for 8 minutes</button>
      <p class="msg" id="pick-msg" role="alert"></p>`;
  }

  async function createHold() {
    const btn = $("hold-btn");
    btn.disabled = true;
    btn.textContent = "Holding…";
    const body = { event_id: S.ev.event_id, access_token: accessToken() };
    if (S.ga) body.ga = { section: S.ga.section, qty: S.ga.qty };
    else body.seats = [...S.picks.values()];
    try {
      S.hold = await api("/holds", { method: "POST", body: JSON.stringify(body) });
      renderHold();
    } catch (e) {
      const d = e.body?.detail || {};
      if (d.code === "WAITING_ROOM") { store.set(`access:${S.ev.event_id}`, null); startQueue(); renderPicker(); return; }
      if (e.status === 409 && d.seats) {
        d.seats.forEach((k) => S.picks.delete(k));
        await openSection(S.open);
      } else {
        renderPicker();
        await refreshAvailability(true);
      }
      const m = $("pick-msg") || $("picker");
      m.textContent = d.message || "That didn't go through. Try again.";
      m.classList.add("err");
    }
  }

  // ------------------------------------------------------------ hold + checkout
  function renderHold() {
    const h = S.hold;
    const cur = h.currency;
    $("panel-browse").hidden = true;
    const p = $("panel-hold");
    p.hidden = false;
    const items = S.ga ? [`${S.ga.qty} × ${S.info[S.ga.section].label}`] : [...S.picks.values()].map((x) => seatLabel(x.section, x.seat));
    p.innerHTML = `
      <div class="timer" id="timer"><span>Your tickets are held for</span><b id="clock">8:00</b></div>
      <h2>Checkout</h2>
      <ul class="summary" style="border:0;padding-top:0;margin-top:0">${items.map((i) => `<li><span>${esc(i)}</span></li>`).join("")}
        <li class="tot"><span>Total</span><span>${money(h.total, cur)}</span></li></ul>
      <label for="email">Email for your tickets</label>
      <input id="email" type="email" autocomplete="email" placeholder="you@example.com" required>
      <label>Payment (simulated, no card needed)</label>
      <div class="pay-opts">
        <label><input type="radio" name="pay" value="simulated_success" checked> Payment succeeds</label>
        <label><input type="radio" name="pay" value="simulated_decline"> Card is declined</label>
      </div>
      <button type="button" class="btn" id="pay-btn" disabled>Pay ${money(h.total, cur)}</button>
      <button type="button" class="btn quiet" id="release-btn">Release these tickets</button>
      <p class="msg" id="pay-msg" role="alert"></p>`;
    $("email").addEventListener("input", (e) => { $("pay-btn").disabled = !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e.target.value); });
    $("pay-btn").onclick = pay;
    $("release-btn").onclick = release;
    const tick = () => {
      const left = Math.max(0, Math.round(h.expires_at - Date.now() / 1000));
      $("clock").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      $("timer").classList.toggle("low", left < 60);
      if (left === 0 && !S.paying) { backToBrowse("Your hold ran out and the tickets went back on sale."); }
    };
    tick();
    S.holdTimer = setInterval(tick, 1000);
    S.timers.push(S.holdTimer);
    p.focus({ preventScroll: true });
    revealOnPhone($("map").parentElement);
  }

  function backToBrowse(message) {
    clearInterval(S.holdTimer);
    S.hold = null; S.picks.clear(); S.ga = null; S.paying = false;
    $("panel-hold").hidden = true;
    $("panel-browse").hidden = false;
    if (S.open) openSection(S.open); else renderPicker();
    refreshAvailability(true);
    if (message) toast(message);
  }

  async function release() {
    try { await api(`/holds/${S.hold.hold_id}`, { method: "DELETE" }); } catch { /* the sweeper will release it */ }
    backToBrowse("Tickets released.");
  }

  const REASONS = {
    PAYMENT_DECLINED: "The payment was declined, so your tickets went back on sale. Pick them again to retry.",
    HOLD_EXPIRED: "Your hold ran out before payment finished. Pick your seats again.",
    HOLD_LOST: "Those seats were released before payment finished. Pick your seats again.",
  };

  async function pay() {
    S.paying = true;
    const btn = $("pay-btn");
    btn.disabled = true;
    btn.textContent = "Paying…";
    $("release-btn").hidden = true;
    const msg = $("pay-msg");
    try {
      const order = await api("/orders", { method: "POST", body: JSON.stringify({
        hold_id: S.hold.hold_id, email: $("email").value.trim(),
        payment: document.querySelector("input[name=pay]:checked").value }) });
      msg.textContent = "Payment received. Confirming your tickets…";
      let result = null;
      for (let i = 0; i < 80 && !result; i++) {
        await new Promise((r) => setTimeout(r, 750));
        const o = await api(`/orders/${order.order_id}`);
        if (o.status !== "PENDING") result = o;
      }
      clearInterval(S.holdTimer);
      if (!result) { msg.textContent = "Still confirming. We'll email you when it's done."; return; }
      if (result.status === "CONFIRMED") return renderStub(result);
      S.paying = false;
      backToBrowse(REASONS[result.reason] || "The order didn't complete. Pick your seats again.");
    } catch (e) {
      S.paying = false;
      const d = e.body?.detail || {};
      if (d.code === "HOLD_EXPIRED") return backToBrowse(REASONS.HOLD_EXPIRED);
      msg.textContent = d.message || "Payment didn't go through. Try again.";
      msg.classList.add("err");
      btn.disabled = false;
      btn.textContent = `Pay ${money(S.hold.total, S.hold.currency)}`;
    }
  }

  function renderStub(o) {
    const ev = S.ev;
    const seats = o.seats.length ? o.seats.map((s) => { const [sec, sk] = s.split("|"); return seatLabel(sec, sk); })
      : o.lines.map((l) => `${l.qty} × ${S.info[l.section].label}`);
    $("panel-hold").innerHTML = `
      <div class="stub">
        <h3>${esc(ev.artist)}</h3>
        <p>${esc(ev.venue)}, ${esc(ev.city)}<br>${esc(when(ev.starts_at, ev.tz))}</p>
        <ul>${seats.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
        <div class="code">${esc(o.ticket_code)}</div>
        <p style="margin-top:8px">Paid ${money(o.total, o.currency)}. A confirmation is on its way to your inbox.</p>
      </div>
      <button type="button" class="btn quiet" id="again-btn">Buy more tickets</button>`;
    $("again-btn").onclick = () => backToBrowse();
    toast("You're going!");
  }

  // ------------------------------------------------------------ live updates
  function connectWs() {
    if (!S.cfg.ws_url) { $("live").hidden = true; return; }
    $("live").hidden = false;
    const ws = new WebSocket(`${S.cfg.ws_url}?event=${encodeURIComponent(S.ev.event_id)}`);
    S.ws = ws;
    ws.onopen = () => { S.wsRetry = 0; $("live").classList.remove("off"); $("live-text").textContent = "Live seat updates"; };
    ws.onclose = () => {
      $("live").classList.add("off");
      $("live-text").textContent = "Reconnecting…";
      S.wsRetry = Math.min(S.wsRetry + 1, 6);
      setTimeout(() => { if (S.ws === ws && S.ev) connectWs(); }, 1000 * 2 ** S.wsRetry);
    };
    ws.onmessage = (m) => {
      let msg;
      try { msg = JSON.parse(m.data); } catch { return; }
      if (msg.type === "avail") {
        for (const [sec, d] of msg.items) S.avail[sec] = (S.avail[sec] ?? 0) + d;
        S.map.update(S.avail);
        renderTiers();
        trace.add({ ws: true, label: `${msg.items.length} section update${msg.items.length === 1 ? "" : "s"}`,
          why: "Someone's hold or purchase changed availability. The change was pushed to every open seat map." });
      } else if (msg.type === "seats" && S.seatGrid && !S.hold) {
        let changed = false;
        for (const [sec, sk, st] of msg.items) {
          if (sec === S.open && S.seatGrid.status[sk] !== st) { S.seatGrid.status[sk] = st; changed = true; }
          if (st !== "a" && S.picks.has(`${sec}|${sk}`)) { S.picks.delete(`${sec}|${sk}`); toast("A seat you picked was just taken."); }
        }
        if (changed) renderPicker();
      }
    };
  }

  // ------------------------------------------------------------ boot
  fetch("/config.json", { cache: "no-store" }).then((r) => (r.ok ? r.json() : {})).catch(() => ({}))
    .then((cfg) => { S.cfg = cfg || {}; window.addEventListener("hashchange", route); route(); });
})();

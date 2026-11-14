// Renders a venue map from static geometry (venues/<id>.json, cached at the
// CloudFront edge) and shades each section by live availability.
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  };

  function render(svg, venue, opts) {
    svg.querySelectorAll(":scope > :not(title)").forEach((n) => n.remove());
    svg.setAttribute("viewBox", venue.viewBox.join(" "));
    const nodes = {};

    for (const d of venue.decor) {
      svg.appendChild(el("path", { d: d.d, class: d.type }));
    }
    const sections = el("g", { class: "sections" });
    const labels = el("g", { class: "labels" });
    for (const s of venue.sections) {
      const info = opts.sections[s.id];
      const onSale = info && info.on_sale;
      const p = el("path", {
        d: s.d, class: "sec" + (onSale ? "" : " killed"), "data-id": s.id,
        fill: onSale ? opts.colors[info.tier] : "",
      });
      if (onSale) {
        p.setAttribute("tabindex", "0");
        p.setAttribute("role", "button");
        p.setAttribute("aria-label", `${info.tier_label} section ${s.label}`);
        p.addEventListener("click", () => opts.onClick(s.id));
        p.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); opts.onClick(s.id); } });
        p.addEventListener("mousemove", (e) => opts.onHover(s.id, e));
        p.addEventListener("focus", () => opts.onHover(s.id, null, p));
        p.addEventListener("mouseleave", opts.onLeave);
        p.addEventListener("blur", opts.onLeave);
      }
      sections.appendChild(p);
      const big = !!s.big;
      labels.appendChild(el("text", { x: s.lx, y: s.ly, class: "lbl" + (big ? " big" : "") + (onSale ? "" : " light") }, s.label));
      nodes[s.id] = p;
    }
    svg.appendChild(sections);
    svg.appendChild(labels);
    for (const d of venue.decor) {
      if (d.type === "stage" && d.label) {
        const t = el("text", { x: d.x, y: d.y, class: "stage-lbl" }, d.label);
        if (d.rotate) t.setAttribute("transform", `rotate(${d.rotate} ${d.x} ${d.y})`);
        svg.appendChild(t);
      }
    }
    return {
      update(avail) {
        for (const [id, node] of Object.entries(nodes)) {
          const info = opts.sections[id];
          if (!info || !info.on_sale) continue;
          const left = avail[id] ?? 0;
          node.classList.toggle("soldout", left <= 0);
          node.style.opacity = left <= 0 ? 1 : (0.45 + 0.55 * Math.min(1, left / info.capacity)).toFixed(2);
        }
      },
      setActive(id) {
        for (const n of Object.values(nodes)) n.classList.remove("active");
        if (id && nodes[id]) nodes[id].classList.add("active");
      },
    };
  }

  window.SeatMap = { render };
})();

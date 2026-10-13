// Dashboard: the grid (GridStack), charts (ECharts), the widget settings dialog, and the formula editor.
(() => {
  const css = getComputedStyle(document.documentElement);
  // Items in calm greens and blues; the surplus always in warm gold so it stands apart
  const palette = ["#0f6e5a", "#14b8a6", "#3b82f6", "#8b5cf6", "#ec4899", "#f97316", "#64748b", "#22c55e"];
  const SURPLUS = T("الفائض");
  const SURPLUS_COLOR = "#f4b740";
  const font = css.getPropertyValue("font-family") || "IBM Plex Sans Arabic";
  const sar = (v) => Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // ---------- charts
  const charts = [];
  document.querySelectorAll(".chart[data-chart]").forEach((el) => {
    const spec = JSON.parse(el.dataset.chart);
    const chart = echarts.init(el, null, { renderer: "svg" });
    const base = { color: palette, textStyle: { fontFamily: font }, tooltip: { valueFormatter: sar } };
    if (spec.type === "pie") {
      let i = 0;
      const data = spec.data.map((d) => ({
        ...d, itemStyle: { color: d.name === SURPLUS ? SURPLUS_COLOR : palette[i++ % palette.length] },
      }));
      const total = data.reduce((s, d) => s + d.value, 0);
      const pct = (v) => (total ? Math.round((v / total) * 1000) / 10 : 0);
      // An HTML legend beside the donut (reads naturally right-to-left), with each share's amount and percent
      const legend = document.createElement("ul");
      legend.className = "donut-legend";
      legend.innerHTML = data.map((d) => `<li><i style="background:${d.itemStyle.color}"></i>
        <span class="donut-name"></span><span class="num">${pct(d.value)}%</span></li>`).join("");
      legend.querySelectorAll(".donut-name").forEach((n, k) => { n.textContent = data[k].name; });
      el.parentElement.classList.add("donut-wrap");
      el.parentElement.appendChild(legend);
      chart.setOption({
        ...base,
        tooltip: { ...base.tooltip, trigger: "item", formatter: (p) => `${p.name}<br><b>${sar(p.value)}</b> ${T("ر.س")} · ${p.percent}%` },
        title: {
          text: sar(total), subtext: T("ر.س · إجمالي الوارد"), left: "center", top: "center", itemGap: 2,
          textStyle: { fontSize: 16, fontWeight: 700, color: "#1c2330", fontFamily: font },
          subtextStyle: { fontSize: 11, color: "#6b7482", fontFamily: font },
        },
        series: [{
          type: "pie", radius: ["62%", "86%"], center: ["50%", "50%"], data, padAngle: 2,
          itemStyle: { borderRadius: 8, borderColor: "#fff", borderWidth: 2 },
          label: { show: false }, labelLine: { show: false },
          emphasis: { scale: true, scaleSize: 5 },
        }],
      });
      legend.querySelectorAll("li").forEach((li, k) => {
        li.addEventListener("mouseenter", () => chart.dispatchAction({ type: "highlight", dataIndex: k }));
        li.addEventListener("mouseleave", () => chart.dispatchAction({ type: "downplay", dataIndex: k }));
      });
      requestAnimationFrame(() => chart.resize());  // the legend narrowed the chart's box
    } else if (spec.type === "bar") {
      chart.setOption({
        ...base,
        tooltip: { ...base.tooltip, trigger: "axis" },
        grid: { left: 8, right: 8, top: 16, bottom: 8, containLabel: true },
        xAxis: { type: "category", data: spec.data.map((d) => d.name), inverse: document.dir === "rtl" },
        yAxis: { type: "value", position: document.dir === "rtl" ? "right" : "left" },
        series: [{ type: "bar", data: spec.data.map((d) => d.value), barMaxWidth: 36, colorBy: "data" }],
      });
    } else {
      chart.setOption({
        ...base,
        tooltip: { ...base.tooltip, trigger: "axis" },
        legend: { bottom: 0 },
        grid: { left: 8, right: 8, top: 16, bottom: 32, containLabel: true },
        xAxis: { type: "category", data: spec.labels, inverse: document.dir === "rtl" },
        yAxis: { type: "value", position: document.dir === "rtl" ? "right" : "left" },
        series: spec.series.map((s) => ({ ...s, type: "line", smooth: true, symbolSize: 6 })),
      });
    }
    charts.push(chart);
  });
  const resizeCharts = () => charts.forEach((c) => c.resize());
  window.addEventListener("resize", resizeCharts);

  // ---------- grid
  const gridEl = document.querySelector(".grid-stack");
  const grid = GridStack.init({ column: 12, cellHeight: 70, margin: 8, staticGrid: true, float: false, rtl: document.dir === "rtl" }, gridEl);
  grid.on("resizestop", resizeCharts);

  const toggle = document.getElementById("toggle-edit");
  if (toggle) {
    let editing = false;
    toggle.addEventListener("click", () => {
      editing = !editing;
      grid.setStatic(!editing);
      gridEl.classList.toggle("editing", editing);
      toggle.textContent = editing ? T("✓ تم") : T("✎ ترتيب العناصر");
    });
    grid.on("change", () => {
      const items = grid.save(false).map((n) => ({ id: n.id, x: n.x, y: n.y, w: n.w, h: n.h }));
      fetch(gridEl.dataset.layoutUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      }).then((r) => { if (!r.ok) alert(T("لم يُحفظ الترتيب، حاول مرة أخرى")); });
      setTimeout(resizeCharts, 50);
    });
  }

  // ---------- widget settings dialog
  const dialog = document.getElementById("widget-dialog");
  if (dialog) {
    const form = document.getElementById("widget-form");
    const kind = document.getElementById("widget-kind");
    const syncFields = () =>
      form.querySelectorAll("[data-for]").forEach((el) => (el.hidden = el.dataset.for !== kind.value));
    kind.addEventListener("change", syncFields);

    document.querySelectorAll("[data-open-widget]").forEach((btn) =>
      btn.addEventListener("click", () => {
        form.reset();
        const isNew = btn.dataset.openWidget === "new";
        form.action = isNew ? form.dataset.addUrl : `/dashboard/widgets/${btn.dataset.openWidget}`;
        document.getElementById("widget-dialog-title").textContent = isNew ? T("إضافة عنصر") : T("تعديل العنصر");
        kind.disabled = !isNew;
        if (!isNew) {
          kind.value = btn.dataset.kind;
          form.elements.title.value = btn.dataset.title;
          const cfg = JSON.parse(btn.dataset.config || "{}");
          for (const [key, value] of Object.entries(cfg)) if (form.elements[key]) form.elements[key].value = value;
        }
        syncFields();
        dialog.showModal();
      })
    );
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  }

  // ---------- formula editor
  const metricForm = document.getElementById("metric-form");
  if (metricForm) {
    const input = metricForm.elements.formula;
    metricForm.querySelectorAll("[data-insert]").forEach((chip) =>
      chip.addEventListener("click", () => {
        const pos = input.selectionStart ?? input.value.length;
        input.value = input.value.slice(0, pos) + chip.dataset.insert + input.value.slice(pos);
        input.focus();
        input.selectionStart = input.selectionEnd = pos + chip.dataset.insert.length;
      })
    );
    metricForm.addEventListener("reset", () => (metricForm.elements.metric_id.value = ""));
    document.querySelectorAll("[data-edit-metric]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const m = JSON.parse(btn.dataset.editMetric);
        metricForm.elements.metric_id.value = m.id;
        metricForm.elements.name.value = m.name;
        metricForm.elements.formula.value = m.formula;
        metricForm.elements.fmt.value = m.fmt;
        metricForm.scrollIntoView({ behavior: "smooth" });
      })
    );
  }
})();

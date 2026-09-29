// اللوحة: الشبكة (GridStack)، الرسوم (ECharts)، نافذة إعداد العنصر، ومحرر المعادلات.
(() => {
  const css = getComputedStyle(document.documentElement);
  const palette = ["#0f6e5a", "#3b82c4", "#d98e04", "#8a5cc2", "#c2410c", "#64748b", "#0e9384"];
  const font = css.getPropertyValue("font-family") || "IBM Plex Sans Arabic";
  const sar = (v) => Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // ---------- الرسوم
  const charts = [];
  document.querySelectorAll(".chart[data-chart]").forEach((el) => {
    const spec = JSON.parse(el.dataset.chart);
    const chart = echarts.init(el, null, { renderer: "svg" });
    const base = { color: palette, textStyle: { fontFamily: font }, tooltip: { valueFormatter: sar } };
    if (spec.type === "pie") {
      chart.setOption({
        ...base,
        tooltip: { ...base.tooltip, trigger: "item" },
        series: [{
          type: "pie", radius: ["38%", "62%"], center: ["50%", "50%"], data: spec.data,
          label: { formatter: "{b}", fontSize: 11 }, labelLine: { length: 6, length2: 6 },
        }],
      });
    } else if (spec.type === "bar") {
      chart.setOption({
        ...base,
        tooltip: { ...base.tooltip, trigger: "axis" },
        grid: { left: 8, right: 8, top: 16, bottom: 8, containLabel: true },
        xAxis: { type: "category", data: spec.data.map((d) => d.name), inverse: true },
        yAxis: { type: "value", position: "right" },
        series: [{ type: "bar", data: spec.data.map((d) => d.value), barMaxWidth: 36, colorBy: "data" }],
      });
    } else {
      chart.setOption({
        ...base,
        tooltip: { ...base.tooltip, trigger: "axis" },
        legend: { bottom: 0 },
        grid: { left: 8, right: 8, top: 16, bottom: 32, containLabel: true },
        xAxis: { type: "category", data: spec.labels, inverse: true },
        yAxis: { type: "value", position: "right" },
        series: spec.series.map((s) => ({ ...s, type: "line", smooth: true, symbolSize: 6 })),
      });
    }
    charts.push(chart);
  });
  const resizeCharts = () => charts.forEach((c) => c.resize());
  window.addEventListener("resize", resizeCharts);

  // ---------- الشبكة
  const gridEl = document.querySelector(".grid-stack");
  const grid = GridStack.init({ column: 12, cellHeight: 70, margin: 8, staticGrid: true, float: false, rtl: true }, gridEl);
  grid.on("resizestop", resizeCharts);

  const toggle = document.getElementById("toggle-edit");
  if (toggle) {
    let editing = false;
    toggle.addEventListener("click", () => {
      editing = !editing;
      grid.setStatic(!editing);
      gridEl.classList.toggle("editing", editing);
      toggle.textContent = editing ? "✓ تم" : "✎ ترتيب العناصر";
    });
    grid.on("change", () => {
      const items = grid.save(false).map((n) => ({ id: n.id, x: n.x, y: n.y, w: n.w, h: n.h }));
      fetch(gridEl.dataset.layoutUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      }).then((r) => { if (!r.ok) alert("ما انحفظ الترتيب، حاول مرة ثانية"); });
      setTimeout(resizeCharts, 50);
    });
  }

  // ---------- نافذة إعداد العنصر
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
        document.getElementById("widget-dialog-title").textContent = isNew ? "إضافة عنصر" : "تعديل العنصر";
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

  // ---------- محرر المعادلات
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

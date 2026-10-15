// Policy editor: add and remove items, switch the unit, and update the rate total live.
(() => {
  const rows = document.getElementById("rows");
  const template = document.getElementById("row-template");
  const total = document.querySelector(".js-pct-total");
  const toNumber = (text) =>
    parseFloat(text.replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d)).replace(/[,٬]/g, "").replace("٫", ".")) || 0;

  function refresh() {
    let sum = 0;
    rows.querySelectorAll(".bucket-row").forEach((tr) => {
      const calc = tr.querySelector(".js-calc").value;
      const isPct = calc !== "fixed_amount";
      tr.querySelector(".js-unit").textContent = isPct ? "%" : T("ر.س");
      const freq = tr.querySelector(".js-freq").value;
      tr.querySelector(".js-day").hidden = freq !== "day_of_month";
      const due = tr.querySelector(".js-due");
      due.hidden = !["quarterly", "semiannual", "annual"].includes(freq);
      due.required = !due.hidden;
      // "+ new IBAN": show the IBAN field and its kind
      const isNew = tr.querySelector(".js-dest").value === "new";
      const box = tr.querySelector(".js-new-dest");
      box.hidden = !isNew;
      box.querySelector('input[name="dest_iban"]').required = isNew;
      // VAT at 15% takes 13.04% of the incoming amount (15 / 115)
      const rate = toNumber(tr.querySelector(".js-value").value);
      if (isPct) sum += calc === "vat_inclusive" ? (rate * 100) / (100 + rate) : rate;
    });
    total.textContent = `${Math.round(sum * 100) / 100}%`;
    total.classList.toggle("negative", sum > 100);
  }

  // The surplus card: show the day/date field for the chosen schedule, and a sentence that follows the choices
  const surplus = document.querySelector(".surplus-card");
  function refreshSurplus() {
    if (!surplus) return;
    const freq = surplus.querySelector(".js-sfreq").value;
    const day = surplus.querySelector(".js-sday");
    const due = surplus.querySelector(".js-sdue");
    day.hidden = freq !== "day_of_month";
    day.required = !day.hidden;
    due.hidden = !["quarterly", "semiannual", "annual"].includes(freq);
    due.required = !due.hidden;
    const date = due.value ? " " + T("بدءًا من {}", due.value) : "";
    const when = {
      weekly: T("كل أسبوع (يوم الأحد)"),
      monthly: T("أول كل شهر"),
      day_of_month: day.value ? T("يوم {} من كل شهر", day.value) : T("في يوم محدد من كل شهر"),
      quarterly: T("كل ثلاثة أشهر{}", date),
      semiannual: T("كل ستة أشهر{}", date),
      annual: T("كل سنة{}", date),
    }[freq];
    const option = surplus.querySelector(".js-sacc").selectedOptions[0];
    surplus.querySelector(".js-surplus-summary").textContent = option && option.value
      ? T("يبقى الفائض في رصيد الشركة ويغطي عجز البنود أولًا، ثم يُحوَّل {} إلى «{}» (••{}).", when, option.dataset.name, option.dataset.iban)
      : T("يبقى الفائض في رصيد الشركة ويغطي عجز البنود أولًا، ولا يُحوَّل تلقائيًا.");
  }
  if (surplus) {
    surplus.addEventListener("input", refreshSurplus);
    surplus.addEventListener("change", refreshSurplus);
    refreshSurplus();
  }

  document.getElementById("add-row").addEventListener("click", () => {
    const tr = template.content.firstElementChild.cloneNode(true);
    const priorities = [...rows.querySelectorAll('input[name="priority"]')].map((i) => +i.value || 0);
    tr.querySelector('input[name="priority"]').value = Math.max(0, ...priorities) + 1;
    rows.appendChild(tr);
    tr.querySelector('input[name="name"]').focus();
    refresh();
  });

  rows.addEventListener("click", (e) => {
    if (e.target.closest(".js-remove")) {
      e.target.closest(".bucket-row").remove();
      refresh();
    }
  });
  rows.addEventListener("input", refresh);
  rows.addEventListener("change", refresh);
  refresh();
})();

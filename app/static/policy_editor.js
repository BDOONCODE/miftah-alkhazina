// منشئ السياسة: إضافة وحذف البنود، وتبديل الوحدة، وحساب مجموع النسب مباشرة.
(() => {
  const rows = document.getElementById("rows");
  const template = document.getElementById("row-template");
  const total = document.querySelector(".js-pct-total");
  const toNumber = (text) =>
    parseFloat(text.replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d)).replace(/[,٬]/g, "").replace("٫", ".")) || 0;

  function refresh() {
    let sum = 0;
    rows.querySelectorAll(".bucket-row").forEach((tr) => {
      const isPct = tr.querySelector(".js-calc").value === "percentage";
      tr.querySelector(".js-unit").textContent = isPct ? "%" : "ر.س";
      const freq = tr.querySelector(".js-freq").value;
      tr.querySelector(".js-day").hidden = freq !== "day_of_month";
      const due = tr.querySelector(".js-due");
      due.hidden = !["quarterly", "semiannual", "annual"].includes(freq);
      due.required = !due.hidden;
      // «+ آيبان جديد»: تظهر خانة الآيبان ونوعه
      const isNew = tr.querySelector(".js-dest").value === "new";
      const box = tr.querySelector(".js-new-dest");
      box.hidden = !isNew;
      box.querySelector('input[name="dest_iban"]').required = isNew;
      if (isPct) sum += toNumber(tr.querySelector(".js-value").value);
    });
    total.textContent = `${Math.round(sum * 100) / 100}%`;
    total.classList.toggle("negative", sum > 100);
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

// كل خانات كلمة المرور: زر العين لإظهار/إخفاء الكلمة.
// الخانات اللي فيها data-rules: قائمة شروط تتحدث مع الكتابة (نفس شروط الخادم في security.py).
(() => {
  // نفس شروط الخادم (security.py). الرموز من لوحة المفاتيح الإنجليزية فقط، فالحروف العربية ما تنحسب رمز
  const RULES = [
    [(p) => p.length >= 8, "8 أحرف على الأقل"],
    [(p) => /[A-Z]/.test(p), "حرف إنجليزي كبير (A-Z)"],
    [(p) => /[a-z]/.test(p), "حرف إنجليزي صغير (a-z)"],
    [(p) => /[0-9]/.test(p), "رقم (0-9)"],
    [(p) => /[!-\/:-@\[-`{-~]/.test(p), "رمز خاص مثل ! @ # $"],
    [(p) => /^[!-~]*$/.test(p), "إنجليزي فقط (بدون عربي أو مسافات)"],
  ];

  // أيقونات خطية (بدون إيموجي) تاخذ لون النص
  const svg = (paths) =>
    `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
  const EYE = svg('<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>');
  const EYE_OFF = svg('<path d="M3 3l18 18"/><path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.1M6.6 6.6C3.9 8.3 2 12 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>');

  document.querySelectorAll('input[type="password"]').forEach((input) => {
    const wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    const eye = document.createElement("button");
    eye.type = "button";
    eye.className = "pw-eye";
    eye.setAttribute("aria-label", "إظهار كلمة المرور");
    eye.innerHTML = EYE;
    eye.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.innerHTML = show ? EYE_OFF : EYE;
      eye.setAttribute("aria-label", show ? "إخفاء كلمة المرور" : "إظهار كلمة المرور");
      input.focus();
    });
    wrap.appendChild(eye);

    if (!input.hasAttribute("data-rules")) return;
    const list = document.createElement("ul");
    list.className = "pw-rules";
    const items = RULES.map(([, text]) => {
      const li = document.createElement("li");
      li.textContent = text;
      list.appendChild(li);
      return li;
    });
    wrap.after(list);
    const check = () => {
      let ok = true;
      RULES.forEach(([passes], i) => {
        const pass = passes(input.value);
        items[i].classList.toggle("ok", pass);
        items[i].classList.toggle("bad", !pass && input.value.length > 0);  // أحمر بعد ما يبدأ يكتب
        ok = ok && pass;
      });
      input.setCustomValidity(ok || !input.value ? "" : "كلمة المرور ما تحقق كل الشروط");
    };
    input.addEventListener("input", check);
    check();
  });
})();

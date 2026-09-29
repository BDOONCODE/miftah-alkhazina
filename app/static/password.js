// كل خانات كلمة المرور: زر العين لإظهار/إخفاء الكلمة.
// الخانات اللي فيها data-rules: قائمة شروط تتحدث مع الكتابة (نفس شروط الخادم في security.py).
(() => {
  const RULES = [
    [/.{8,}/s, "8 أحرف على الأقل"],
    [/[A-Z]/, "حرف إنجليزي كبير (A-Z)"],
    [/[a-z]/, "حرف إنجليزي صغير (a-z)"],
    [/\d/, "رقم (0-9)"],
    [/[^A-Za-z0-9\s]/, "رمز خاص مثل ! @ # $"],
  ];

  document.querySelectorAll('input[type="password"]').forEach((input) => {
    const wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    const eye = document.createElement("button");
    eye.type = "button";
    eye.className = "pw-eye";
    eye.setAttribute("aria-label", "إظهار كلمة المرور");
    eye.textContent = "👁";
    eye.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.textContent = show ? "🙈" : "👁";
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
      RULES.forEach(([re], i) => {
        const pass = re.test(input.value);
        items[i].classList.toggle("ok", pass);
        ok = ok && pass;
      });
      input.setCustomValidity(ok || !input.value ? "" : "كلمة المرور ما تحقق كل الشروط");
    };
    input.addEventListener("input", check);
    check();
  });
})();

// IBAN fields: strip spaces and uppercase while typing or pasting, and stop at 24 characters (SA + 22 digits).
// maxlength alone would cut off a pasted IBAN written with spaces, so the input is cleaned first.
(() => {
  const clean = (input) => {
    const value = input.value.replace(/\s+/g, "").toUpperCase().slice(0, 24);
    if (value !== input.value) input.value = value;
  };
  document.addEventListener("input", (e) => {
    if (e.target.matches && e.target.matches("input[data-iban]")) clean(e.target);
  });
  document.addEventListener("paste", (e) => {
    const input = e.target;
    if (!(input.matches && input.matches("input[data-iban]"))) return;
    e.preventDefault();
    const pasted = (e.clipboardData || window.clipboardData).getData("text");
    input.value = (input.value.slice(0, input.selectionStart) + pasted + input.value.slice(input.selectionEnd));
    clean(input);
  });
})();

// Copy buttons: data-copy="<id of the input to copy>". Falls back to selecting the text.
document.addEventListener("click", async (e) => {
  const button = e.target.closest && e.target.closest("[data-copy]");
  if (!button) return;
  const input = document.getElementById(button.dataset.copy);
  if (!input) return;
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(input.value);
    button.textContent = "تم النسخ ✓";
  } catch {
    input.select();
    button.textContent = "انسخ النص المحدد";
  }
  setTimeout(() => (button.textContent = label), 2000);
});

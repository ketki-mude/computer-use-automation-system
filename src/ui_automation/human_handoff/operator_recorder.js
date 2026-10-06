// Records what a person does on the live session while they hold control. Installed in
// every frame; the Python side keeps an event only while a human holds the lease, so the
// automation's own clicks are never recorded as human actions. Typed values are never
// sent: only their length, so a one-time code or password cannot leak into evidence.
(() => {
  if (window.__uiAutomationRecorderInstalled) return;
  window.__uiAutomationRecorderInstalled = true;
  const describe = (el) => (window.__uiAutomation ? window.__uiAutomation.describe(el) : { tag: el.tagName.toLowerCase() });
  const send = (event) => { if (typeof window.__uiAutomationOperator === "function") window.__uiAutomationOperator(event); };
  document.addEventListener("click", (e) => {
    const el = e.target.closest && e.target.closest("a,button,input,select,textarea,[onclick]");
    if (el && !["text", "password", "textarea", "select-one"].includes(el.type)) send({ type: "click", target: describe(el) });
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    const select = el.tagName === "SELECT";
    send({
      type: select ? "select" : "fill",
      target: describe(el),
      value: select ? (el.selectedOptions[0] ? el.selectedOptions[0].text : "") : "•".repeat(String(el.value || "").length),
    });
  }, true);
})();

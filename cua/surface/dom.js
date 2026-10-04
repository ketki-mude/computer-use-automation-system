// Injected into every frame. One source of truth for how the system reads a screen:
// observe() builds the numbered outline the LLM sees during discovery, and resolve()
// finds elements from recorded strategies during replay. Because both use the same
// role, name, label and table logic, a locator recorded from an observation means the
// same thing when it is replayed.
(() => {
  if (window.__cua) return;

  const SKIP = new Set(["SCRIPT", "STYLE", "HEAD", "NOSCRIPT", "TEMPLATE", "META", "LINK", "TITLE"]);
  const BLOCK = new Set(["DIV", "P", "TABLE", "TBODY", "THEAD", "TFOOT", "TR", "FORM", "H1", "H2",
    "H3", "H4", "H5", "H6", "UL", "OL", "LI", "BR", "HR", "CENTER", "BODY", "FIELDSET", "PRE",
    "BLOCKQUOTE", "DL", "DT", "DD", "SECTION", "HEADER", "FOOTER", "NAV", "MAIN", "ARTICLE"]);
  const CONTROL_ROLES = new Set(["link", "button", "textbox", "combobox", "listbox", "checkbox",
    "radio", "menuitem", "tab"]);
  const CONTROL_SELECTOR = "a,button,input,select,textarea,[onclick],[role]";

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  // "Member Number:" and "Member Number" are the same label.
  const strip = (s) => norm(s).replace(/[\s:]+$/, "");
  const same = (a, b) => strip(a).toLowerCase() === strip(b).toLowerCase();

  function visible(el) {
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function role(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const t = el.tagName;
    if (t === "A") return "link";
    if (t === "BUTTON") return "button";
    if (t === "SELECT") return el.multiple ? "listbox" : "combobox";
    if (t === "TEXTAREA") return "textbox";
    if (t === "INPUT") {
      const ty = (el.type || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(ty)) return "button";
      if (ty === "checkbox" || ty === "radio") return ty;
      if (ty === "hidden") return "";
      return "textbox";
    }
    if (t === "TD" || t === "TH") return "cell";
    if (el.hasAttribute("onclick")) return "button";
    return "";
  }

  const isControl = (el) => CONTROL_ROLES.has(role(el));

  function name(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return norm(aria);
    const t = el.tagName;
    if (t === "INPUT") {
      const ty = (el.type || "").toLowerCase();
      if (["submit", "button", "reset"].includes(ty)) return norm(el.value);
      if (ty === "image") return norm(el.alt || el.title);
    }
    if (["INPUT", "SELECT", "TEXTAREA"].includes(t)) {
      if (el.labels && el.labels.length) return norm(el.labels[0].innerText);
      return norm(el.title || el.placeholder || "");
    }
    if (t === "IMG") return norm(el.alt || el.title);
    return norm(el.innerText || el.textContent).slice(0, 80);
  }

  // The visible text a person would read as this control's label: a real <label>, else
  // the nearest text cell before it in the same table row, else the text just before it.
  function derivedLabel(el) {
    if (el.labels && el.labels.length) return strip(el.labels[0].innerText);
    const cell = el.closest("td,th");
    if (cell) {
      for (let c = cell.previousElementSibling; c; c = c.previousElementSibling) {
        if (c.querySelector(CONTROL_SELECTOR)) continue;
        const t = strip(c.innerText);
        if (t) return t;
      }
    }
    let n = el;
    for (let depth = 0; depth < 3 && n; depth++) {
      for (let p = n.previousSibling; p; p = p.previousSibling) {
        const t = strip(p.textContent);
        if (t) return t.slice(-60);
      }
      n = n.parentElement;
    }
    return "";
  }

  // ---------------------------------------------------------------- tables

  function colIndex(cell) {
    let i = 0;
    for (let c = cell.previousElementSibling; c; c = c.previousElementSibling) i += c.colSpan || 1;
    return i;
  }

  function cellAt(tr, idx) {
    let i = 0;
    for (const c of tr.cells) {
      if (idx >= i && idx < i + (c.colSpan || 1)) return c;
      i += c.colSpan || 1;
    }
    return null;
  }

  // A header row is one where every non-empty cell is a <th> or entirely bold.
  function isHeaderRow(tr) {
    const cells = Array.from(tr.cells).filter((c) => strip(c.innerText));
    if (cells.length < 2) return false;
    return cells.every((c) => {
      if (c.tagName === "TH") return true;
      const b = c.querySelector("b,strong");
      return b && same(b.innerText, c.innerText);
    });
  }

  function headerOf(table) {
    const first = table.rows[0];
    return first && isHeaderRow(first) ? first : null;
  }

  function headerIndex(hdr, column) {
    for (const c of hdr.cells) if (same(c.innerText, column)) return colIndex(c);
    return -1;
  }

  const innermostTables = () =>
    Array.from(document.querySelectorAll("table")).filter((t) => !t.querySelector("table"));

  function tableContext(el) {
    const cell = el.closest("td,th");
    if (!cell) return {};
    const tr = cell.parentElement;
    const table = tr.closest("table");
    const hdr = headerOf(table);
    const ctx = { col: colIndex(cell), pos: Array.from(tr.cells).indexOf(cell), row_texts: Array.from(tr.cells).map((c) => strip(c.innerText)) };
    if (hdr && hdr !== tr && !table.querySelector("table")) {
      ctx.header = Array.from(hdr.cells).map((c) => strip(c.innerText));
      const h = cellAt(hdr, ctx.col);
      ctx.column = h ? strip(h.innerText) : "";
      // For each column: is this row's value unique within the table? Unique columns can key the row.
      const body = Array.from(table.rows).filter((r) => r !== hdr);
      ctx.unique_columns = ctx.header.filter((col, i) => {
        const mine = cellAt(tr, i);
        if (!mine || !strip(mine.innerText)) return false;
        return body.filter((r) => { const c = cellAt(r, i); return c && same(c.innerText, mine.innerText); }).length === 1;
      });
    }
    return ctx;
  }

  function describe(el) {
    const r = el.getBoundingClientRect();
    return Object.assign({
      tag: el.tagName.toLowerCase(),
      role: role(el),
      name: name(el),
      label: isControl(el) && ["textbox", "combobox", "listbox", "checkbox", "radio"].includes(role(el)) ? derivedLabel(el) : "",
      type: (el.type || "").toLowerCase(),
      text: norm(el.innerText || "").slice(0, 160),
      value: el.type === "password" ? "" : (el.value == null ? "" : String(el.value)),
      attr_name: el.getAttribute("name") || "",
      id: el.id || "",
      href: el.getAttribute("href") || "",
      onclick: el.getAttribute("onclick") || "",
      onchange: el.getAttribute("onchange") || "",
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    }, tableContext(el));
  }

  // ---------------------------------------------------------------- observe

  function observe(startRef) {
    document.querySelectorAll("[data-cua-ref]").forEach((e) => e.removeAttribute("data-cua-ref"));
    let ref = startRef;
    const elements = [];
    const lines = [];
    let cur = [];
    const flush = () => { const l = norm(cur.join(" ")); if (l) lines.push(l); cur = []; };

    function addRef(el, kind) {
      const r = ref++;
      el.setAttribute("data-cua-ref", String(r));
      elements.push(Object.assign(describe(el), { ref: r, kind }));
      return r;
    }

    function controlText(el, r) {
      const ro = role(el), nm = name(el);
      let s = `[${r}] ${ro}`;
      if (nm) s += ` "${nm}"`;
      if (["textbox", "combobox", "listbox", "checkbox", "radio"].includes(ro)) {
        const lab = derivedLabel(el);
        if (lab && !same(lab, nm)) s += ` (label: "${lab}")`;
      }
      if (ro === "textbox") s += el.type === "password" ? ` value="${el.value ? "••••" : ""}"` : ` value="${norm(el.value)}"`;
      if (ro === "combobox" || ro === "listbox") {
        const sel = el.selectedOptions && el.selectedOptions[0];
        s += ` selected="${sel ? norm(sel.text) : ""}" options=[${Array.from(el.options).map((o) => `"${norm(o.text)}"`).join(", ")}]`;
      }
      if (ro === "checkbox" || ro === "radio") s += el.checked ? " checked" : " unchecked";
      return s;
    }

    function walk(node, inline) {
      if (node.nodeType === 3) { const t = norm(node.textContent); if (t) cur.push(t); return; }
      if (node.nodeType !== 1) return;
      const el = node;
      if (SKIP.has(el.tagName) || (el.tagName === "INPUT" && el.type === "hidden")) return;
      if (!visible(el)) return;
      if (isControl(el)) { cur.push(controlText(el, addRef(el, "control"))); return; }
      if (!inline && el.tagName === "TR" && !el.querySelector("table")) { flush(); lines.push(renderRow(el)); return; }
      const block = !inline && BLOCK.has(el.tagName);
      if (block) flush();
      for (const ch of el.childNodes) walk(ch, inline);
      if (block) flush();
    }

    // A table row becomes one line: "| text | [12]text | [13] link "View" |".
    // Text-only cells get a number too, so the LLM can point at a value to extract.
    function renderRow(tr) {
      const parts = Array.from(tr.cells).filter(visible).map((cell) => {
        const saved = cur;
        cur = [];
        const text = norm(cell.innerText);
        if (!cell.querySelector(CONTROL_SELECTOR) && text) cur.push(`[${addRef(cell, "cell")}]${text}`);
        else for (const ch of cell.childNodes) walk(ch, true);
        const out = norm(cur.join(" "));
        cur = saved;
        return out;
      });
      return "| " + parts.join(" | ") + " |";
    }

    if (document.body) walk(document.body, false);
    flush();
    return { lines, elements, next_ref: ref, title: document.title, url: location.href };
  }

  // ---------------------------------------------------------------- resolve

  function innermostWithText(text) {
    return Array.from(document.body.querySelectorAll("*")).filter((e) =>
      !SKIP.has(e.tagName) && visible(e) && same(e.innerText, text) &&
      !Array.from(e.children).some((c) => same(c.innerText, text)));
  }

  function controlsIn(root, wantRole) {
    return Array.from(root.querySelectorAll(CONTROL_SELECTOR)).filter((e) =>
      visible(e) && isControl(e) && (!wantRole || role(e) === wantRole));
  }

  function matchRows(m) {
    const rows = [];
    for (const table of innermostTables()) {
      const hdr = headerOf(table);
      if (!hdr) continue;
      const idx = headerIndex(hdr, m.column);
      if (idx < 0) continue;
      for (const tr of table.rows) {
        if (tr === hdr) continue;
        const c = cellAt(tr, idx);
        if (c && same(c.innerText, m.equals)) rows.push(tr);
      }
    }
    return rows;
  }

  function resolveLabelAnchor(spec) {
    const hits = [];
    for (const labelEl of innermostWithText(spec.label)) {
      const cell = labelEl.closest("td,th");
      if (cell) {
        for (let c = cell.nextElementSibling; c; c = c.nextElementSibling) {
          if (spec.role === "cell") {
            if (!c.querySelector(CONTROL_SELECTOR) && strip(c.innerText)) { hits.push(c); break; }
          } else {
            const found = controlsIn(c, spec.role);
            if (found.length) { hits.push(found[0]); break; }
          }
        }
      } else if (spec.role !== "cell") {
        const all = controlsIn(document, spec.role);
        const next = all.find((e) => labelEl.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING);
        if (next) hits.push(next);
      }
    }
    return hits;
  }

  function resolve(spec, token) {
    let hits = [];
    switch (spec.kind) {
      case "role_name":
        hits = controlsIn(document, spec.role).filter((e) => same(name(e), spec.name));
        break;
      case "label_anchor":
        hits = resolveLabelAnchor(spec);
        break;
      case "table_row":
        hits = matchRows(spec.row).flatMap((tr) => controlsIn(tr, spec.role).filter((e) => same(name(e), spec.name)));
        break;
      case "table_cell":
        for (const tr of matchRows(spec.row)) {
          const hdr = headerOf(tr.closest("table"));
          const c = hdr && cellAt(tr, headerIndex(hdr, spec.column));
          if (c) hits.push(c);
        }
        break;
      case "text":
        hits = innermostWithText(spec.text).filter((e) => !spec.role || role(e) === spec.role);
        break;
      case "css":
        try { hits = Array.from(document.querySelectorAll(spec.selector)).filter(visible); } catch (e) { hits = []; }
        break;
    }
    hits = Array.from(new Set(hits));
    document.querySelectorAll("[data-cua-hit]").forEach((e) => e.removeAttribute("data-cua-hit"));
    hits.forEach((e) => e.setAttribute("data-cua-hit", token));
    return {
      count: hits.length,
      refs: hits.map((e) => e.getAttribute("data-cua-ref")),
      element: hits.length === 1 ? describe(hits[0]) : null,
    };
  }

  // Mark elements whose text or value contains a sensitive string, for screenshot masking.
  function markSensitive(values) {
    document.querySelectorAll("[data-cua-mask]").forEach((e) => e.removeAttribute("data-cua-mask"));
    if (!values.length || !document.body) return 0;
    let n = 0;
    for (const e of document.body.querySelectorAll("td,th,input,textarea,span,b,font,div,p")) {
      if (e.querySelector("td,th,div,p")) continue;
      const text = (e.value || "") + " " + (e.innerText || "");
      if (values.some((v) => text.includes(v))) { e.setAttribute("data-cua-mask", "1"); n++; }
    }
    return n;
  }

  window.__cua = {
    observe,
    resolve,
    describe,
    markSensitive,
    text: () => (document.body ? document.body.innerText : ""),
    title: () => document.title,
  };
})();

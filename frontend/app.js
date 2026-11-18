"use strict";
/* GestPro Cloud — interfaz web.
 * Todas las llamadas van al API Gateway (HAProxy) por la misma URL: /api/<servicio>/...
 * El token JWT se guarda en sessionStorage y se envía en cada petición.
 */
const $ = (sel, root = document) => root.querySelector(sel);
const COP = new Intl.NumberFormat("es-CO", { style: "currency", currency: "COP", maximumFractionDigits: 0 });
const money = (v) => COP.format(v || 0);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const state = {
  token: sessionStorage.getItem("token"),
  user: JSON.parse(sessionStorage.getItem("user") || "null"),
  sede: Number(localStorage.getItem("sede")) || null,
  sedes: [],
  cart: [],
  products: [],
};
const isAdmin = () => state.user?.role === "admin";

// ── Cliente HTTP ────────────────────────────────────────────
async function api(method, path, body, { form = false, quiet = false } = {}) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let payload;
  if (body !== undefined) {
    if (form) payload = body;
    else { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  }
  const resp = await fetch(path, { method, headers, body: payload });
  const replica = resp.headers.get("X-Replica");
  if (replica && !quiet) $("#last-replica").textContent = replica;
  if (resp.status === 401 && path !== "/api/auth/login") { logout(); throw new Error("La sesión expiró"); }
  const isJson = (resp.headers.get("Content-Type") || "").includes("json");
  const data = isJson ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const detail = data && data.detail;
    const msg = Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : detail || `Error ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return { data, replica };
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 4500);
}

// ── Sesión ──────────────────────────────────────────────────
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const { data } = await api("POST", "/api/auth/login", { username: f.get("username"), password: f.get("password") });
    state.token = data.access_token;
    state.user = data.user;
    sessionStorage.setItem("token", state.token);
    sessionStorage.setItem("user", JSON.stringify(state.user));
    $("#login-error").textContent = "";
    await startApp();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

function logout() {
  sessionStorage.clear();
  state.token = null;
  state.user = null;
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}
$("#logout").addEventListener("click", logout);

// ── Navegación ──────────────────────────────────────────────
const VIEWS = {
  pos: { title: "Punto de venta", render: viewPOS },
  inventario: { title: "Inventario por sede", render: viewInventory },
  clientes: { title: "Clientes y créditos", render: viewClients },
  caja: { title: "Caja y gastos", render: viewCash },
  facturas: { title: "Facturación electrónica DIAN", render: viewInvoices },
  reportes: { title: "Reportes y MapReduce", render: viewReports },
  monitoreo: { title: "Monitoreo", render: viewMonitoring },
};

async function startApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#user-chip").innerHTML = `${esc(state.user.username)}<small>${esc(state.user.role)}</small>`;
  $("#nav").innerHTML = Object.entries(VIEWS).map(([k, v]) => `<a href="#/${k}" data-view="${k}">${v.title}</a>`).join("");
  const { data } = await api("GET", "/api/inventory/locations");
  state.sedes = data;
  if (!state.sedes.some((s) => s.id === state.sede)) state.sede = state.sedes[0]?.id;
  $("#sede-select").innerHTML = state.sedes.map((s) =>
    `<option value="${s.id}" ${s.id === state.sede ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  route();
}

$("#sede-select").addEventListener("change", (e) => {
  state.sede = Number(e.target.value);
  localStorage.setItem("sede", state.sede);
  state.cart = [];
  route();
});
window.addEventListener("hashchange", route);

async function route() {
  if (!state.token) return;
  const key = (location.hash.replace("#/", "") || "pos").split("?")[0];
  const view = VIEWS[key] || VIEWS.pos;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === key));
  $("#view-title").textContent = view.title;
  const el = $("#view");
  el.innerHTML = `<p class="muted">Cargando…</p>`;
  try {
    await view.render(el);
  } catch (err) {
    el.innerHTML = `<div class="card"><h2>No se pudo cargar</h2><p class="error">${esc(err.message)}</p>
      <p class="muted">Si un microservicio está caído, el resto de la plataforma sigue funcionando.</p></div>`;
  }
}

const sedeName = () => state.sedes.find((s) => s.id === state.sede)?.name || "";

async function currentSession() {
  try {
    return (await api("GET", `/api/cash/sessions/current?location_id=${state.sede}`, undefined, { quiet: true })).data;
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

function openCashForm(el, onDone) {
  el.innerHTML = `<div class="card" style="max-width:460px">
      <h2>Abrir caja en ${esc(sedeName())}</h2>
      <p class="muted">Para vender necesitas un turno de caja abierto (igual que en GestPro de escritorio).</p>
      <form id="open-form" class="grid">
        <label>Efectivo inicial <input name="initial" type="number" min="0" step="1000" value="100000"></label>
        <button class="btn primary">Abrir caja</button>
      </form></div>`;
  $("#open-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/api/cash/sessions/open", { location_id: state.sede, initial_cash: Number(e.target.initial.value) });
      toast("Caja abierta");
      onDone();
    } catch (err) { toast(err.message, true); }
  });
}

// ── Punto de venta ──────────────────────────────────────────
async function viewPOS(el) {
  const caja = await currentSession();
  if (!caja) return openCashForm(el, route);
  let clientes = [];
  try { clientes = (await api("GET", "/api/clients", undefined, { quiet: true })).data; } catch { /* servicio caído */ }

  el.innerHTML = `<div class="grid cols-2">
    <div class="card">
      <div class="row"><h2 style="flex:3">Productos en ${esc(sedeName())}</h2>
        <input id="search" placeholder="Buscar producto…" style="flex:2"></div>
      <div id="products" class="products"></div>
    </div>
    <div class="grid">
      <div class="card">
        <h2>Venta · caja #${caja.id}</h2>
        <div id="cart"></div>
        <div class="total"><span>Total</span><span id="total">$0</span></div>
        <form id="checkout" class="grid">
          <div class="row">
            <label>Pago <select name="payment">
              <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
              <option value="transferencia">Transferencia</option><option value="credito">Crédito</option></select></label>
            <label>Descuento <input name="discount" type="number" min="0" step="100" value="0"></label>
          </div>
          <label>Cliente <select name="client"><option value="">Consumidor final</option>
            ${clientes.map((c) => `<option value="${c.id}">${esc(c.name)} · deuda ${money(c.debt)}</option>`).join("")}</select></label>
          <label class="check"><input type="checkbox" name="invoice"> Emitir factura electrónica DIAN (Factus)</label>
          <div id="customer" class="row hidden">
            <label>Tipo <select name="doc_type"><option>CC</option><option>NIT</option><option>CE</option></select></label>
            <label>Documento <input name="identification" value="222222222222"></label>
            <label>Nombre <input name="cname" value="Consumidor Final"></label>
            <label>Correo <input name="email" type="email" value="consumidor@final.com"></label>
          </div>
          <button class="btn primary" id="pay-btn">Cobrar</button>
        </form>
      </div>
      <div class="card hidden" id="timeline-card">
        <h2>Qué pasó con la venta</h2>
        <ul id="timeline" class="timeline"></ul>
      </div>
    </div></div>`;

  const form = $("#checkout");
  form.invoice.addEventListener("change", () => $("#customer").classList.toggle("hidden", !form.invoice.checked));
  form.client.addEventListener("change", () => {
    const c = clientes.find((x) => String(x.id) === form.client.value);
    if (c) {
      form.doc_type.value = c.doc_type || "CC";
      form.identification.value = c.document || "222222222222";
      form.cname.value = c.name;
      form.email.value = c.email || "consumidor@final.com";
    }
  });
  let timer;
  $("#search").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => loadProducts(e.target.value), 250); });
  form.addEventListener("submit", (e) => { e.preventDefault(); checkout(form, caja, clientes); });
  await loadProducts("");
  renderCart();
}

async function loadProducts(search) {
  const { data } = await api("GET", `/api/inventory/stock?location_id=${state.sede}&search=${encodeURIComponent(search)}`);
  state.products = data.items;
  $("#products").innerHTML = state.products.map((p) => `
    <button class="product ${p.low ? "low" : ""}" data-id="${p.product_id}" ${p.stock <= 0 ? "disabled" : ""}>
      <div class="name">${esc(p.name)}</div>
      <div class="stock">${esc(p.category)} · stock ${p.stock} ${p.low ? '<span class="chip warn">bajo</span>' : ""}</div>
      <div class="price">${money(p.price)}</div>
    </button>`).join("") || `<p class="muted">Sin productos. Importa el catálogo desde Inventario.</p>`;
  document.querySelectorAll(".product").forEach((b) => b.addEventListener("click", () => addToCart(Number(b.dataset.id))));
}

function addToCart(id) {
  const p = state.products.find((x) => x.product_id === id);
  const line = state.cart.find((l) => l.product_id === id);
  if (line) {
    if (line.quantity >= p.stock) return toast(`Solo hay ${p.stock} unidades de ${p.name}`, true);
    line.quantity++;
  } else state.cart.push({ product_id: id, name: p.name, price: p.price, quantity: 1, stock: p.stock });
  renderCart();
}

function renderCart() {
  const box = $("#cart");
  if (!box) return;
  box.innerHTML = state.cart.map((l, i) => `
    <div class="cart-line"><div>${esc(l.name)}<div class="muted">${money(l.price)} c/u</div></div>
      <div class="qty"><button data-i="${i}" data-d="-1">−</button>${l.quantity}<button data-i="${i}" data-d="1">+</button></div>
      <strong>${money(l.price * l.quantity)}</strong></div>`).join("") || `<p class="muted">Toca un producto para agregarlo.</p>`;
  box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const l = state.cart[b.dataset.i];
    l.quantity += Number(b.dataset.d);
    if (l.quantity > l.stock) l.quantity = l.stock;
    if (l.quantity <= 0) state.cart.splice(b.dataset.i, 1);
    renderCart();
  }));
  $("#total").textContent = money(state.cart.reduce((s, l) => s + l.price * l.quantity, 0));
}

async function checkout(form, caja, clientes) {
  if (!state.cart.length) return toast("El carrito está vacío", true);
  const payment = form.payment.value;
  const clientId = form.client.value ? Number(form.client.value) : null;
  if (payment === "credito" && !clientId) return toast("Una venta a crédito necesita un cliente", true);
  const body = {
    location_id: state.sede, payment_method: payment, discount: Number(form.discount.value) || 0,
    client_id: clientId, session_id: caja.id, invoice: form.invoice.checked,
    items: state.cart.map((l) => ({ product_id: l.product_id, quantity: l.quantity })),
    customer: form.invoice.checked ? { doc_type: form.doc_type.value, identification: form.identification.value,
      name: form.cname.value, email: form.email.value } : null,
  };
  // "Antes" de la venta, para detectar cuándo llegan los efectos asíncronos
  const first = state.cart[0];
  const antes = {
    stock: first.stock, qty: first.quantity, name: first.name, product_id: first.product_id,
    caja: (await currentSession())?.num_sales ?? 0,
    deuda: clientes.find((c) => c.id === clientId)?.debt ?? 0,
  };
  $("#pay-btn").disabled = true;
  const t0 = performance.now();
  try {
    const { data: venta, replica } = await api("POST", "/api/sales", body);
    state.cart = [];
    renderCart();
    toast(`Venta #${venta.id} registrada por ${money(venta.total)}`);
    runTimeline(venta, replica, antes, t0);
    loadProducts($("#search").value);
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("#pay-btn").disabled = false;
  }
}

// Muestra en vivo los efectos asíncronos del evento sale.created
function runTimeline(venta, replica, antes, t0) {
  $("#timeline-card").classList.remove("hidden");
  const ul = $("#timeline");
  ul.innerHTML = "";
  const paso = (titulo) => {
    const li = document.createElement("li");
    li.className = "wait";
    li.innerHTML = `<span class="dot">•</span><div>${titulo}<small></small></div><span class="ms"></span>`;
    ul.appendChild(li);
    return {
      ok(detalle) { li.className = "done"; li.querySelector(".dot").textContent = "✓"; this.info(detalle); li.querySelector(".ms").textContent = `${Math.round(performance.now() - t0)} ms`; },
      fail(detalle) { li.className = "fail"; li.querySelector(".dot").textContent = "!"; this.info(detalle); },
      info(detalle) { li.querySelector("small").innerHTML = detalle || ""; },
    };
  };
  paso(`<b>pos-sales</b> guardó la venta #${venta.id} y publicó <code>sale.created</code> en Redis`)
    .ok(`Síncrono · atendida por ${esc(replica)} · ${money(venta.total)} (${esc(venta.payment_method)})`);

  const esperar = async (p, comprobar, segundos = 40) => {
    const fin = performance.now() + segundos * 1000;
    while (performance.now() < fin) {
      try { if (await comprobar(p)) return; } catch { /* el servicio puede estar caído: seguimos esperando */ }
      await sleep(700);
    }
    p.fail("Aún no llega. El evento sigue pendiente en el stream y se procesará cuando el servicio vuelva.");
  };

  esperar(paso(`<b>catalog-inventory</b> descontó el stock en ${esc(venta.location_name)}`), async (p) => {
    const { data } = await api("GET", `/api/inventory/products/${antes.product_id}?location_id=${venta.location_id}`, undefined, { quiet: true });
    if (data.stock > antes.stock - antes.qty) return false;
    p.ok(`${esc(antes.name)}: ${antes.stock} → ${data.stock}${data.stock <= data.min_stock ? ' <span class="chip warn">stock bajo: se publicó stock.low</span>' : ""}`);
    return true;
  });
  if (venta.session_id) {
    esperar(paso(`<b>cash-expenses</b> registró la venta en la caja #${venta.session_id}`), async (p) => {
      const caja = await currentSession();
      if (!caja || caja.num_sales <= antes.caja) return false;
      p.ok(`Efectivo esperado en caja: ${money(caja.expected_cash)}`);
      return true;
    });
  }
  if (venta.payment_method === "credito") {
    esperar(paso(`<b>clients-credits</b> sumó la venta a la deuda del cliente`), async (p) => {
      const { data } = await api("GET", `/api/clients/${venta.client_id}`, undefined, { quiet: true });
      if (data.debt < antes.deuda + venta.total - 1) return false;
      p.ok(`${esc(data.name)}: deuda ${money(antes.deuda)} → ${money(data.debt)}`);
      return true;
    });
  }
  if (venta.invoice_requested) {
    esperar(paso(`<b>billing-factus</b> emitió la factura electrónica en Factus (DIAN)`), async (p) => {
      let f;
      try { f = (await api("GET", `/api/billing/invoices/by-sale/${venta.id}`, undefined, { quiet: true })).data; }
      catch (err) { if (err.status === 404) return false; throw err; }
      if (f.dian_status === "emitida" || f.dian_status === "simulada") {
        p.ok(`Factura <b>${esc(f.number)}</b>${f.dian_status === "simulada" ? " (modo simulado)" : ""}<br>CUFE: ${esc(f.cufe.slice(0, 48))}…` +
          (f.pdf_url ? `<br><a href="${esc(f.pdf_url)}" target="_blank" rel="noopener">Ver documento</a>` : "") +
          (f.qr_url ? ` · <a href="${esc(f.qr_url)}" target="_blank" rel="noopener">QR DIAN</a>` : ""));
        return true;
      }
      if (f.dian_status === "error") p.info(`Factus respondió error (intento ${f.attempts}); se reintenta automáticamente: ${esc(f.error.slice(0, 120))}`);
      return false;
    }, 120);
  }
}

// ── Inventario ──────────────────────────────────────────────
async function viewInventory(el) {
  const { data } = await api("GET", `/api/inventory/stock?location_id=${state.sede}`);
  const items = data.items;
  const bajos = items.filter((p) => p.low).length;
  const valor = items.reduce((s, p) => s + p.price * Math.max(p.stock, 0), 0);
  let categorias = [];
  if (isAdmin()) categorias = (await api("GET", "/api/inventory/categories", undefined, { quiet: true })).data;
  el.innerHTML = `
    <div class="grid cols-3">
      <div class="kpi"><div class="label">Productos en ${esc(sedeName())}</div><div class="value">${items.length}</div></div>
      <div class="kpi"><div class="label">En stock bajo</div><div class="value">${bajos}</div></div>
      <div class="kpi accent"><div class="label">Valor del inventario (precio venta)</div><div class="value">${money(valor)}</div></div>
    </div>
    ${isAdmin() ? `<div class="grid cols-3" style="margin-top:18px">
      <div class="card"><h2>Importar catálogo desde Excel</h2>
        <form id="import-form" class="grid"><input type="file" name="file" accept=".xlsx" required>
        <button class="btn primary">Importar a ${esc(sedeName())}</button></form>
        <p class="muted">Columnas: Nombre, Categoría, PrecioVenta, PrecioCompra, Cantidad, StockMinimo.</p></div>
      <div class="card"><h2>Nuevo producto</h2>
        <form id="product-form" class="grid">
          <input name="name" placeholder="Nombre" required>
          <div class="row"><select name="category">${categorias.map((c) => `<option>${esc(c)}</option>`).join("")}</select>
            <input name="price" type="number" min="1" placeholder="Precio" required></div>
          <div class="row"><input name="stock" type="number" min="0" placeholder="Stock" value="10">
            <input name="min_stock" type="number" min="0" placeholder="Mínimo" value="5"></div>
          <button class="btn">Crear producto</button></form></div>
      <div class="card"><h2>Nueva sede</h2>
        <form id="sede-form" class="grid"><input name="name" placeholder="Nombre de la sede" required>
          <input name="address" placeholder="Dirección"><button class="btn">Crear sede</button></form></div>
    </div>` : ""}
    <div class="card" style="margin-top:18px">
      <table><thead><tr><th>Producto</th><th>Categoría</th><th class="num">Precio</th><th class="num">Stock</th>
        <th class="num">Mínimo</th><th>Estado</th></tr></thead>
      <tbody>${items.map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.category)}</td><td class="num">${money(p.price)}</td>
        <td class="num">${p.stock}</td><td class="num">${p.min_stock}</td>
        <td>${p.low ? '<span class="chip warn">Stock bajo</span>' : '<span class="chip ok">OK</span>'}</td></tr>`).join("")}</tbody></table>
    </div>`;
  if (!isAdmin()) return;
  $("#import-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    fd.append("location_id", state.sede);
    try {
      const { data: r } = await api("POST", "/api/inventory/products/import", fd, { form: true });
      toast(`Excel importado en ${r.sede}: ${r.importados} productos nuevos, ${r.stock_en_sede} con stock cargado en la sede, ` +
        `${r.duplicados} ya existían, ${r.errores} errores`);
      route();
    } catch (err) { toast(err.message, true); }
  });
  $("#product-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("POST", "/api/inventory/products", { name: f.name.value, category: f.category.value,
        price: Number(f.price.value), stock: Number(f.stock.value), min_stock: Number(f.min_stock.value), location_id: state.sede });
      toast("Producto creado");
      route();
    } catch (err) { toast(err.message, true); }
  });
  $("#sede-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/api/inventory/locations", { name: e.target.name.value, address: e.target.address.value });
      toast("Sede creada");
      startApp();
    } catch (err) { toast(err.message, true); }
  });
}

// ── Clientes ────────────────────────────────────────────────
async function viewClients(el) {
  const { data: clientes } = await api("GET", "/api/clients");
  const deuda = clientes.reduce((s, c) => s + c.debt, 0);
  el.innerHTML = `
    <div class="grid cols-2">
      <div class="card"><h2>Clientes</h2>
        <table><thead><tr><th>Nombre</th><th>Documento</th><th>Teléfono</th><th class="num">Deuda</th><th></th></tr></thead>
        <tbody>${clientes.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.doc_type)} ${esc(c.document)}</td><td>${esc(c.phone)}</td>
          <td class="num">${c.debt > 0 ? `<span class="chip warn">${money(c.debt)}</span>` : money(0)}</td>
          <td>${c.debt > 0 ? `<button class="btn small" data-pay="${c.id}">Abonar</button>` : ""}</td></tr>`).join("")
          || '<tr><td colspan="5" class="muted">Aún no hay clientes</td></tr>'}</tbody></table></div>
      <div class="grid">
        <div class="kpi accent"><div class="label">Cartera total (deuda de clientes)</div><div class="value">${money(deuda)}</div></div>
        <div class="card"><h2>Nuevo cliente</h2>
          <form id="client-form" class="grid">
            <input name="name" placeholder="Nombre completo" required>
            <div class="row"><select name="doc_type"><option>CC</option><option>NIT</option><option>CE</option></select>
              <input name="document" placeholder="Documento"></div>
            <div class="row"><input name="phone" placeholder="Teléfono"><input name="email" type="email" placeholder="Correo"></div>
            <button class="btn primary">Crear cliente</button></form></div>
      </div>
    </div>`;
  $("#client-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("POST", "/api/clients", { name: f.name.value, doc_type: f.doc_type.value, document: f.document.value,
        phone: f.phone.value, email: f.email.value });
      toast("Cliente creado");
      route();
    } catch (err) { toast(err.message, true); }
  });
  el.querySelectorAll("[data-pay]").forEach((b) => b.addEventListener("click", async () => {
    const monto = Number(prompt("Valor del abono (COP):"));
    if (!monto) return;
    const caja = await currentSession();
    try {
      await api("POST", `/api/clients/${b.dataset.pay}/payments`, { amount: monto, session_id: caja?.id ?? null });
      toast(`Abono registrado${caja ? ` en la caja #${caja.id} (vía evento client.payment)` : ""}`);
      route();
    } catch (err) { toast(err.message, true); }
  }));
}

// ── Caja y gastos ───────────────────────────────────────────
async function viewCash(el) {
  const caja = await currentSession();
  if (!caja) return openCashForm(el, route);
  const { data: historial } = await api("GET", "/api/cash/sessions?limit=10", undefined, { quiet: true });
  el.innerHTML = `
    <div class="grid cols-4">
      <div class="kpi"><div class="label">Base inicial</div><div class="value">${money(caja.initial_cash)}</div></div>
      <div class="kpi"><div class="label">Ventas en efectivo (${caja.num_sales} ventas)</div><div class="value">${money(caja.cash_sales)}</div></div>
      <div class="kpi"><div class="label">Abonos − gastos</div><div class="value">${money(caja.payments - caja.expenses)}</div></div>
      <div class="kpi accent"><div class="label">Efectivo esperado</div><div class="value">${money(caja.expected_cash)}</div></div>
    </div>
    <p class="muted">Otras formas de pago (no entran al cajón): ${money(caja.other_sales)} · Caja #${caja.id} abierta ${esc(caja.opened_at.replace("T", " "))}</p>
    <div class="grid cols-2">
      <div class="card"><h2>Registrar gasto</h2>
        <form id="expense-form" class="grid">
          <div class="row"><select name="category"><option>Servicios</option><option>Proveedores</option><option>Arriendo</option>
            <option>Nómina</option><option>Transporte</option><option>Otros</option></select>
            <input name="amount" type="number" min="1" placeholder="Valor" required></div>
          <input name="description" placeholder="Descripción">
          <label class="check"><input type="checkbox" name="cash" checked> Sale del efectivo de la caja</label>
          <button class="btn">Registrar gasto</button></form></div>
      <div class="card"><h2>Cerrar caja (cuadre)</h2>
        <form id="close-form" class="grid">
          <label>Efectivo contado <input name="actual" type="number" min="0" value="${Math.round(caja.expected_cash)}"></label>
          <input name="notes" placeholder="Observaciones">
          <button class="btn dark">Cerrar caja</button></form></div>
    </div>
    <div class="card" style="margin-top:18px"><h2>Historial de cajas</h2>
      <table><thead><tr><th>#</th><th>Usuario</th><th>Apertura</th><th>Estado</th><th class="num">Esperado</th>
        <th class="num">Contado</th><th class="num">Diferencia</th></tr></thead>
      <tbody>${historial.map((h) => `<tr><td>${h.id}</td><td>${esc(h.user_name)}</td><td>${esc(h.opened_at.replace("T", " "))}</td>
        <td>${h.status === "open" ? '<span class="chip info">abierta</span>' : '<span class="chip ok">cerrada</span>'}</td>
        <td class="num">${money(h.expected_cash)}</td><td class="num">${h.status === "closed" ? money(h.actual_cash) : "—"}</td>
        <td class="num">${h.status === "closed" ? money(h.difference) : "—"}</td></tr>`).join("")}</tbody></table></div>`;
  $("#expense-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("POST", "/api/cash/expenses", { category: f.category.value, amount: Number(f.amount.value),
        description: f.description.value, location_id: state.sede, session_id: f.cash.checked ? caja.id : null });
      toast("Gasto registrado");
      route();
    } catch (err) { toast(err.message, true); }
  });
  $("#close-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const { data: r } = await api("POST", `/api/cash/sessions/${caja.id}/close`,
        { actual_cash: Number(e.target.actual.value), notes: e.target.notes.value });
      toast(`Caja cerrada. Esperado ${money(r.expected_cash)}, contado ${money(r.actual_cash)}, diferencia ${money(r.difference)}`);
      route();
    } catch (err) { toast(err.message, true); }
  });
}

// ── Facturas ────────────────────────────────────────────────
const CHIP_FACTURA = { emitida: "ok", simulada: "info", pendiente: "warn", error: "bad" };
async function viewInvoices(el) {
  const [{ data: facturas }, { data: estado }] = await Promise.all([
    api("GET", "/api/billing/invoices"), api("GET", "/api/billing/status", undefined, { quiet: true })]);
  el.innerHTML = `
    <p class="muted">Modo: <b>${esc(estado.mode)}</b> · API: ${esc(estado.environment)} ·
      ${estado.configured ? '<span class="chip ok">credenciales OK (Docker secret)</span>' : '<span class="chip bad">sin credenciales</span>'}</p>
    <div class="card"><table><thead><tr><th>Venta</th><th>Fecha</th><th>Cliente</th><th class="num">Total</th><th>Estado DIAN</th>
      <th>Número</th><th>CUFE</th><th>Documento</th><th></th></tr></thead>
    <tbody>${facturas.map((f) => `<tr><td>#${f.sale_id}</td><td>${esc(f.date)} ${esc(f.time)}</td><td>${esc(f.customer_name)}</td>
      <td class="num">${money(f.total)}</td>
      <td><span class="chip ${CHIP_FACTURA[f.dian_status] || "info"}" title="${esc(f.error)}">${esc(f.dian_status)}</span>
        ${f.attempts > 1 ? `<small class="muted">${f.attempts} intentos</small>` : ""}</td>
      <td>${esc(f.number)}</td><td title="${esc(f.cufe)}"><code>${esc(f.cufe.slice(0, 14))}${f.cufe ? "…" : ""}</code></td>
      <td>${f.pdf_url ? `<a href="${esc(f.pdf_url)}" target="_blank" rel="noopener">Ver</a>` : ""}
        ${f.qr_url ? ` · <a href="${esc(f.qr_url)}" target="_blank" rel="noopener">QR</a>` : ""}</td>
      <td>${f.dian_status === "error" && isAdmin() ? `<button class="btn small" data-retry="${f.id}">Reintentar</button>` : ""}</td></tr>`).join("")
      || '<tr><td colspan="9" class="muted">Aún no hay facturas. Marca "Emitir factura electrónica" al cobrar.</td></tr>'}</tbody></table></div>`;
  el.querySelectorAll("[data-retry]").forEach((b) => b.addEventListener("click", async () => {
    try {
      await api("POST", `/api/billing/invoices/${b.dataset.retry}/retry`);
      toast("Factura emitida");
      route();
    } catch (err) { toast(err.message, true); }
  }));
}

// ── Reportes (CQRS + MapReduce) ─────────────────────────────
async function viewReports(el) {
  const { data: r } = await api("GET", "/api/sales/summary?days=30");
  el.innerHTML = `
    <div class="grid cols-3">
      <div class="kpi"><div class="label">Ventas (30 días)</div><div class="value">${r.num_sales}</div></div>
      <div class="kpi"><div class="label">Ingresos (30 días)</div><div class="value">${money(r.revenue)}</div></div>
      <div class="kpi accent"><div class="label">Ticket promedio</div><div class="value">${money(r.num_sales ? r.revenue / r.num_sales : 0)}</div></div>
    </div>
    <div class="grid cols-2" style="margin-top:18px">
      <div class="card"><h2>Ventas por día y sede <span class="chip info">modelo de lectura CQRS</span></h2>
        <table><thead><tr><th>Fecha</th><th>Sede</th><th class="num">Ventas</th><th class="num">Ingresos</th></tr></thead>
        <tbody>${r.by_day.slice().reverse().map((d) => `<tr><td>${esc(d.date)}</td><td>${esc(d.location)}</td>
          <td class="num">${d.num_sales}</td><td class="num">${money(d.revenue)}</td></tr>`).join("")
          || '<tr><td colspan="4" class="muted">Sin ventas</td></tr>'}</tbody></table></div>
      <div class="card" id="mapreduce"><h2>Consolidado por sede y categoría <span class="chip info">MapReduce</span></h2>
        <p class="muted">Cargando…</p></div>
    </div>`;
  renderMapReduce();
}

async function renderMapReduce() {
  const box = $("#mapreduce");
  let job = null;
  try {
    job = (await api("GET", "/api/mapreduce/jobs/latest", undefined, { quiet: true })).data;
  } catch (err) {
    if (err.status !== 404 || err.message === "Ruta no encontrada") {
      box.innerHTML = `<h2>Consolidado por sede y categoría <span class="chip info">MapReduce</span></h2>
        <p class="muted">El módulo MapReduce aún no está desplegado.</p>`;
      return;
    }
  }
  const n = (v) => (v ?? 0).toLocaleString("es-CO");
  const FASES = { starting: "iniciando", extract: "1 · extract (ventas reales → bronze)", map: "2 · map en paralelo (bronze → silver)",
    reduce: "3 · shuffle + reduce", done: "terminado" };
  let resumen = "Aún no se ha ejecutado ningún job.";
  if (job && job.status === "running") {
    const pct = job.partitions ? Math.round((100 * job.partitions_done) / job.partitions) : 0;
    resumen = `<b>En curso:</b> ${FASES[job.phase] || esc(job.phase)} · particiones ${job.partitions_done}/${job.partitions} (${pct} %)
      ${job.retries ? ` · <span class="chip warn">${job.retries} reintentadas (worker caído)</span>` : ""}`;
  } else if (job && job.status === "done") {
    resumen = `<b>${n(job.rows_in)}</b> líneas en ${job.partitions} particiones (${job.bronze_mb} MB bronze, ${n(job.real_rows)} ventas reales) ·
      <b>${job.workers}</b> workers · total <b>${job.duration_s.toFixed(2)} s</b> (map ${job.map_s} s, reduce ${job.reduce_s} s) ·
      ${n(job.rows_discarded)} filas sucias descartadas
      ${job.retries ? ` · <span class="chip warn">${job.retries} particiones reintentadas</span>` : ""}<br>` +
      Object.entries(job.worker_stats || {}).map(([w, s]) =>
        `<span class="chip info">${esc(w)}: ${s.partitions} particiones · ${n(s.rows)} líneas</span>`).join(" ");
  } else if (job && job.status === "failed") {
    resumen = `<span class="chip bad">falló</span> ${esc(job.error)}`;
  }
  const filas = job?.status === "done" ? job.results : [];
  box.innerHTML = `<h2>Consolidado por sede y categoría <span class="chip info">MapReduce</span></h2>
    <div class="row"><p class="muted" style="flex:4">${resumen}</p>
      <button class="btn primary" id="mr-run" ${job?.status === "running" ? "disabled" : ""}>Ejecutar MapReduce</button></div>
    <table><thead><tr><th>Sede</th><th>Categoría</th><th class="num">Líneas</th><th class="num">Ingresos</th>
      <th class="num">Ticket prom.</th><th class="num">Desv. est.</th></tr></thead>
    <tbody>${filas.map((f) => `<tr><td>${esc(f.sede)}</td><td>${esc(f.categoria)}</td><td class="num">${n(f.lineas)}</td>
      <td class="num">${money(f.ingresos)}</td><td class="num">${money(f.ticket_promedio)}</td><td class="num">${money(f.desviacion)}</td></tr>`).join("")}</tbody></table>
    <p class="muted">Capa gold: <code>gold/consolidado_sede_categoria.csv</code> · ticket promedio = promedio por línea de venta.</p>`;
  if (job?.status === "running") { setTimeout(renderMapReduce, 700); return; }
  $("#mr-run").addEventListener("click", async () => {
    try {
      await api("POST", "/api/mapreduce/jobs", {});
      toast("Job MapReduce lanzado: los workers procesan las particiones en paralelo");
      renderMapReduce();
    } catch (err) { toast(err.message, true); }
  });
}

// ── Monitoreo ───────────────────────────────────────────────
async function viewMonitoring(el) {
  const host = location.hostname;
  el.innerHTML = `<div class="grid cols-3 links">
    <a href="http://${host}:3000" target="_blank" rel="noopener">Grafana<span>Dashboard de negocio e infraestructura (réplicas, latencia, eventos, facturas)</span></a>
    <a href="http://${host}:8404/stats" target="_blank" rel="noopener">HAProxy stats<span>Estado de cada réplica detrás del gateway (health checks)</span></a>
    <a href="http://${host}:9090/targets" target="_blank" rel="noopener">Prometheus<span>Targets descubiertos por DNS de Swarm (tasks.&lt;servicio&gt;)</span></a>
  </div>`;
}

// Arranque: si ya había sesión, entra directo
if (state.token && state.user) startApp().catch(logout);

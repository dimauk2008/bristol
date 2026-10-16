const $ = (sel, root = document) => root.querySelector(sel);

const PAGE = 60;
const NEW_BADGE_HOURS = 24;
const FLASH_MINUTES = 6;

const state = {
  sources: [],
  activeSourceId: null,
  view: localStorage.getItem("avito.view") || "table",
  listings: [],
  listingsTotal: 0,
  notifs: [],
  notifsTotal: 0,
  scans: [],
  stats: null,
  q: "",
  sort: "recent",
  fresh: localStorage.getItem("avito.fresh") || "24h",
  active: true,
  unreadOnly: false,
  tab: "listings",
  ws: null,
  wsOpen: false,
  reconnectDelay: 1000,
  scanning: false,
  lastRefresh: 0,
  listingsSeq: 0,
  notifsSeq: 0,
  firstPageIds: new Set(),
  enterIds: new Set(),
  flashIds: new Set(),
  pendingNew: 0,
  bannerTimer: null,
};

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toDate(value) {
  if (!value) return null;
  let iso = value;
  if (typeof iso === "string" && iso.length > 10) {
    const hasZone = iso.endsWith("Z") || /[+-]\d{2}:?\d{2}$/.test(iso.slice(10));
    if (!hasZone) iso += "Z";
  }
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatPrice(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("ru-RU").format(value) + " ₽";
}

function formatDateTime(value) {
  const date = toDate(value);
  if (!date) return "—";
  return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function relTime(value) {
  const date = toDate(value);
  if (!date) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
  if (minutes < 1) return "только что";
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} дн назад`;
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
}

function hoursSince(value) {
  const date = toDate(value);
  return date ? (Date.now() - date.getTime()) / 3600000 : Infinity;
}

function isNewListing(item) {
  return hoursSince(item.first_seen) < NEW_BADGE_HOURS;
}

function isFlash(item) {
  return hoursSince(item.first_seen) < FLASH_MINUTES / 60;
}

function plural(n, one, few, many) {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

function listingsQuery(offset = 0, limit = PAGE) {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    sort: state.sort,
    fresh: state.fresh,
    active: String(state.active),
  });
  if (state.activeSourceId !== null) params.set("source_id", String(state.activeSourceId));
  if (state.q) params.set("q", state.q);
  return params;
}

function showSkeleton() {
  const box = $("#listings-box");
  box.replaceChildren(
    h(
      "div",
      { class: "skel-list" },
      ...Array.from({ length: 8 }, () =>
        h(
          "div",
          { class: "skel-row" },
          h("div", { class: "skel skel-img" }),
          h("div", { class: "skel skel-title" }),
          h("div", { class: "skel skel-price" }),
          h("div", { class: "skel skel-meta" })
        )
      )
    )
  );
  $("#listings-empty").classList.add("hidden");
  $("#listings-more-wrap").classList.add("hidden");
}

function hideNewBanner() {
  state.pendingNew = 0;
  clearTimeout(state.bannerTimer);
  state.bannerTimer = null;
  $("#new-banner").classList.add("hidden");
}

function showNewBanner(count, { top = false } = {}) {
  const banner = $("#new-banner");
  const noun = plural(count, "новое объявление", "новых объявления", "новых объявлений");
  $("#new-banner-text").textContent = top
    ? `+${count} ${noun} вверху списка`
    : `+${count} ${noun}`;
  $("#new-banner-btn").textContent = top ? "К списку" : "Показать";
  banner.dataset.mode = top ? "top" : "reload";
  banner.classList.remove("hidden");
  banner.classList.remove("enter");
  void banner.offsetWidth;
  banner.classList.add("enter");
  clearTimeout(state.bannerTimer);
  state.bannerTimer = setTimeout(hideNewBanner, 20000);
}

function thumb(src, cls) {
  if (!src) return h("div", { class: cls || "thumb-empty", text: "A" });
  return h("img", {
    src,
    alt: "",
    loading: "lazy",
    onerror: (event) => event.target.replaceWith(h("div", { class: cls || "thumb-empty", text: "A" })),
  });
}

function priceCell(item) {
  const delta = item.prev_price !== null && item.price !== null && item.price !== item.prev_price ? item.price - item.prev_price : null;
  return h(
    "span",
    {},
    h("span", { class: "price-main", text: formatPrice(item.price) }),
    delta ? h("span", { class: "price-old", text: formatPrice(item.prev_price) }) : null,
    delta ? h("span", { class: `delta ${delta > 0 ? "up" : "down"}`, text: `${delta > 0 ? "▲" : "▼"} ${formatPrice(Math.abs(delta))}` }) : null
  );
}

function statusTags(item) {
  const tags = [];
  if (isNewListing(item)) tags.push(h("span", { class: "tag new", text: "NEW" }));
  if (!item.is_active) tags.push(h("span", { class: "tag off", text: "пропал" }));
  if (item.price_changes > 0) tags.push(h("span", { class: "tag plain", text: `цен: ${item.price_changes}` }));
  return tags;
}

function toast({ title, text, image, kind = "info", onClick, timeout = 7000 }) {
  const node = h(
    "div",
    { class: `toast ${kind}` },
    image ? h("img", { src: image, alt: "" }) : null,
    h(
      "div",
      { class: "toast-body" },
      h("div", { class: "toast-title", text: title || "" }),
      text ? h("div", { class: "toast-text", text }) : null
    ),
    h("button", { class: "toast-close", text: "×", onclick: (event) => { event.stopPropagation(); node.remove(); } })
  );
  if (onClick) {
    node.style.cursor = "pointer";
    node.addEventListener("click", () => { onClick(); node.remove(); });
  }
  $("#toasts").append(node);
  setTimeout(() => {
    node.classList.add("out");
    setTimeout(() => node.remove(), 220);
  }, timeout);
  return node;
}

function activeSource() {
  return state.sources.find((source) => source.id === state.activeSourceId) || null;
}

function renderChips() {
  const box = $("#source-chips");
  box.replaceChildren(
    ...state.sources.map((source) =>
      h(
        "button",
        {
          type: "button",
          class: `chip${source.id === state.activeSourceId ? " active" : ""}${source.is_enabled ? "" : " disabled"}`,
          onclick: () => setActiveSource(source.id),
        },
        source.name,
        source.unread_count ? h("span", { class: "chip-count", text: String(source.unread_count) }) : null
      )
    )
  );
  const source = activeSource();
  $("#brand-url").textContent = source ? source.url : "нет активного списка";
}

async function loadSources() {
  try {
    state.sources = await api("/api/sources");
  } catch (error) {
    toast({ title: "Не удалось загрузить списки", text: error.message, kind: "error" });
    return;
  }
  const remembered = Number(localStorage.getItem("avito.source"));
  const exists = state.sources.some((source) => source.id === remembered);
  if (!exists || state.activeSourceId === null) {
    const preferred = state.sources.find((source) => source.is_enabled) || state.sources[0];
    state.activeSourceId = preferred ? preferred.id : null;
  }
  if (exists) state.activeSourceId = remembered;
  renderChips();
}

async function setActiveSource(id) {
  if (state.activeSourceId === id) return;
  state.activeSourceId = id;
  localStorage.setItem("avito.source", String(id));
  renderChips();
  fillSourceSettings();
  await refreshActiveSource(true);
}

async function loadStats() {
  if (state.activeSourceId === null) return;
  try {
    applyStats(await api(`/api/stats?source_id=${state.activeSourceId}`));
  } catch (error) {
    toast({ title: "Ошибка статистики", text: error.message, kind: "error" });
  }
}

function bumpStat(selector, value) {
  const node = $(selector);
  const next = value === null || value === undefined ? "—" : String(value);
  if (node.textContent !== next) {
    node.textContent = next;
    node.classList.remove("bump");
    void node.offsetWidth;
    node.classList.add("bump");
  }
}

function applyStats(stats) {
  if (!stats) return;
  state.stats = stats;
  bumpStat("#stat-active", stats.active_listings);
  bumpStat("#stat-total", stats.total_listings);
  bumpStat("#stat-new24", stats.new_last_24h);
  bumpStat("#stat-unread", stats.unread_notifications ?? 0);
  setBadge(stats.unread_notifications || 0);
  $("#count-listings").textContent = stats.active_listings ? String(stats.active_listings) : "";
  $("#count-notifications").textContent = stats.unread_notifications ? String(stats.unread_notifications) : "";
  renderStatus();
}

function setBadge(count) {
  const badge = $("#bell-badge");
  const prev = Number(badge.textContent) || 0;
  if (!count) {
    badge.classList.add("hidden");
    badge.textContent = "0";
    return;
  }
  badge.classList.remove("hidden");
  badge.textContent = count > 99 ? "99+" : String(count);
  if (count > prev) {
    badge.classList.remove("bump");
    void badge.offsetWidth;
    badge.classList.add("bump");
  }
}

function renderStatus() {
  const pill = $("#scan-status");
  const text = $("#scan-status-text");
  const stats = state.stats;
  if (state.scanning) {
    pill.dataset.state = "running";
    text.textContent = state.scanProgress || "Сканирую…";
    return;
  }
  const last = stats && stats.last_scan;
  const lastAgo = last ? relTime(last.finished_at || last.started_at) || "—" : null;
  if (last && last.status === "error") {
    pill.dataset.state = "error";
    text.textContent = `Ошибка: ${(last.message || "").slice(0, 50)} · скан ${lastAgo}`;
    return;
  }
  pill.dataset.state = "idle";
  if (!stats || !stats.next_scan_at) {
    text.textContent = last ? `Скан ${lastAgo}` : "Ожидание первого скана";
    return;
  }
  const nextDate = toDate(stats.next_scan_at);
  const delta = nextDate ? nextDate.getTime() - Date.now() : 0;
  if (delta <= 0) {
    text.textContent = "Сканирую скоро…";
    return;
  }
  const total = Math.round(delta / 1000);
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  text.textContent = `Скан ${lastAgo} · следующий через ${mm}:${ss}`;
}

async function loadListings(reset = false) {
  const seq = ++state.listingsSeq;
  const prevLen = state.listings.length;
  const offset = reset ? 0 : state.listings.length;
  if (reset) {
    state.listings = [];
    state.listingsTotal = 0;
    state.firstPageIds = new Set();
    state.enterIds = new Set();
    hideNewBanner();
    if (prevLen === 0) showSkeleton();
  }
  try {
    const data = await api(`/api/listings?${listingsQuery(offset)}`);
    if (seq !== state.listingsSeq) return;
    state.listingsTotal = data.total;
    if (reset) {
      state.listings = data.items.slice();
      state.firstPageIds = new Set(data.items.map((item) => item.id));
    } else {
      state.listings.push(...data.items);
    }
    renderListings();
  } catch (error) {
    if (seq === state.listingsSeq) toast({ title: "Ошибка загрузки товаров", text: error.message, kind: "error" });
  }
}

async function syncListings() {
  if (state.activeSourceId === null) return;
  if (!state.listings.length) {
    await loadListings(true);
    return;
  }
  const seq = ++state.listingsSeq;
  let data;
  try {
    data = await api(`/api/listings?${listingsQuery(0)}`);
  } catch (_) {
    return;
  }
  if (seq !== state.listingsSeq) return;

  const prevFirst = state.firstPageIds;
  const nextIds = data.items.map((item) => item.id);
  const nextFirst = new Set(nextIds);
  const newItems = data.items.filter((item) => !prevFirst.has(item.id));
  const removed = new Set([...prevFirst].filter((id) => !nextFirst.has(id)));

  const byId = new Map(state.listings.map((item) => [item.id, item]));
  for (const raw of data.items) {
    const local = byId.get(raw.id);
    if (!local) continue;
    if (local.price !== raw.price) state.flashIds.add(raw.id);
    Object.assign(local, raw);
  }
  if (removed.size) state.listings = state.listings.filter((item) => !removed.has(item.id));

  const canPrepend = !state.q && (state.sort === "recent" || state.sort === "published");
  if (newItems.length) {
    if (canPrepend) {
      const fresh = new Set(newItems.map((item) => item.id));
      state.enterIds = new Set(newItems.map((item) => item.id));
      state.listings = [...newItems, ...state.listings.filter((item) => !fresh.has(item.id))];
      hideNewBanner();
      if (window.scrollY > 260) showNewBanner(newItems.length, { top: true });
    } else {
      state.pendingNew += newItems.length;
      showNewBanner(state.pendingNew);
    }
  } else if (!canPrepend) {
    hideNewBanner();
  }

  state.listingsTotal = data.total;
  state.firstPageIds = nextFirst;
  state.lastRefresh = Date.now();
  renderListings();
  renderUpdated();
}

function headerCell(text, sortValue, className) {
  const isSorted = sortValue && state.sort === sortValue;
  return h(
    "th",
    {
      class: [className || "", sortValue ? "sortable" : "", isSorted ? "sorted" : ""].join(" ").trim(),
      onclick: sortValue ? () => applySort(sortValue) : null,
    },
    text,
    isSorted ? " ▾" : null
  );
}

function applySort(sortValue) {
  state.sort = sortValue;
  $("#sort-select").value = sortValue;
  loadListings(true);
}

function tableView(items) {
  const rows = items.map((item) => {
    const tags = statusTags(item);
    const classes = [
      isNewListing(item) ? "row-new" : "",
      isFlash(item) ? "row-flash" : "",
      state.enterIds.has(item.id) ? "row-enter" : "",
      state.flashIds.has(item.id) ? "price-flash" : "",
    ]
      .filter(Boolean)
      .join(" ");
    return h(
      "tr",
      { class: classes },
      h("td", { class: "col-img" }, thumb(item.image)),
      h("td", { class: "col-title" }, h("a", { href: item.url, target: "_blank", rel: "noopener", text: item.title })),
      h("td", { class: "num" }, priceCell(item)),
      h("td", {}, item.location || h("span", { class: "dim", text: "—" })),
      h("td", {}, item.date_text || h("span", { class: "dim", text: relTime(item.first_seen) })),
      h("td", { class: "status-cell" }, tags.length ? tags : h("span", { class: "dim", text: "—" })),
      h("td", { class: "col-link" }, h("a", { class: "btn btn-sm", href: item.url, target: "_blank", rel: "noopener", text: "Открыть" }))
    );
  });

  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "data-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", { class: "col-img" }, "Фото"),
          headerCell("Название", "title"),
          headerCell("Цена", state.sort === "price_asc" ? "price_desc" : "price_asc", "num"),
          h("th", {}, "Место"),
          headerCell("Дата", "published"),
          h("th", {}, "Статус"),
          h("th", { class: "col-link" }, "")
        )
      ),
      h("tbody", {}, rows)
    )
  );
}

function cardsView(items) {
  return h(
    "div",
    { class: "grid" },
    ...items.map((item) =>
      h(
        "article",
        {
          class: [
            "card",
            isNewListing(item) ? "is-new" : "",
            item.is_active ? "" : "is-off",
            state.enterIds.has(item.id) ? "enter" : "",
            state.flashIds.has(item.id) ? "price-flash" : "",
          ]
            .filter(Boolean)
            .join(" "),
        },
        h(
          "div",
          { class: "card-media" },
          item.image
            ? h("img", { src: item.image, alt: "", loading: "lazy", onerror: (event) => event.target.replaceWith(h("div", { class: "no-image", text: "A" })) })
            : h("div", { class: "no-image", text: "A" }),
          isNewListing(item) ? h("span", { class: "flag", text: "NEW" }) : null,
          item.is_active ? null : h("span", { class: "flag off", text: "ПРОПАЛ" })
        ),
        h(
          "div",
          { class: "card-body" },
          h("div", { class: "card-title" }, h("a", { href: item.url, target: "_blank", rel: "noopener", text: item.title })),
          h("div", { class: "price-row" }, priceCell(item)),
          h(
            "div",
            { class: "card-meta" },
            h("span", { text: [item.location, item.date_text].filter(Boolean).join(" · ") || "—" }),
            item.price_changes ? h("span", { text: `цен: ${item.price_changes}` }) : null
          )
        )
      )
    )
  );
}

function compactView(items) {
  return h(
    "div",
    { class: "compact-list" },
    ...items.map((item) =>
      h(
        "div",
        {
          class: [
            "compact-row",
            isNewListing(item) ? "row-new" : "",
            state.enterIds.has(item.id) ? "enter" : "",
            state.flashIds.has(item.id) ? "price-flash" : "",
          ]
            .filter(Boolean)
            .join(" "),
        },
        thumb(item.image),
        h("span", { class: "c-title" }, h("a", { href: item.url, target: "_blank", rel: "noopener", text: item.title })),
        h("span", { class: "c-price" }, formatPrice(item.price)),
        h("span", { class: "c-meta" }, [item.location, item.date_text].filter(Boolean).join(" · ") || "—"),
        item.is_active ? null : h("span", { class: "tag off", text: "пропал" })
      )
    )
  );
}

function renderListings() {
  const box = $("#listings-box");
  const items = state.listings;
  const scrollY = window.scrollY;
  box.replaceChildren();
  $("#listings-empty").classList.toggle("hidden", items.length > 0);
  $("#listings-more-wrap").classList.toggle("hidden", items.length >= state.listingsTotal);
  if (!items.length) {
    if (state.fresh !== "all" && state.listingsTotal === 0 && !state.q) {
      $("#listings-empty").textContent = "За выбранный период объявлений нет — расширьте период или запустите скан";
    } else {
      $("#listings-empty").textContent = state.sources.length ? "Ничего не найдено" : "Списков пока нет — нажмите «+ Список»";
    }
    return;
  }
  if (state.view === "table") box.append(tableView(items));
  else if (state.view === "cards") box.append(cardsView(items));
  else box.append(compactView(items));
  if (scrollY > 0 && document.activeElement === document.body) window.scrollTo(0, scrollY);
  state.enterIds = new Set();
  state.flashIds = new Set();
}

function renderViewSwitch() {
  document.querySelectorAll("#view-switch button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === state.view);
  });
}

async function loadNotifications(reset = false) {
  const seq = ++state.notifsSeq;
  if (reset) {
    state.notifs = [];
    state.notifsTotal = 0;
  }
  const params = new URLSearchParams({
    limit: "40",
    offset: String(state.notifs.length),
    unread: String(state.unreadOnly),
  });
  if (state.activeSourceId !== null) params.set("source_id", String(state.activeSourceId));
  try {
    const data = await api(`/api/notifications?${params}`);
    if (seq !== state.notifsSeq) return;
    state.notifsTotal = data.total;
    state.notifs.push(...data.items);
    $("#notif-total-label").textContent = `всего: ${data.total}, непрочитанных: ${data.unread}`;
    renderNotifications();
  } catch (error) {
    if (seq === state.notifsSeq) toast({ title: "Ошибка уведомлений", text: error.message, kind: "error" });
  }
}

function notifNode(item) {
  const isPrice = item.type === "price_change";
  const meta = [
    h("span", { class: `pill${isPrice ? " price" : ""}`, text: isPrice ? "Смена цены" : "Новое объявление" }),
    isPrice && item.old_price !== null && item.price !== null
      ? h("span", { text: `${formatPrice(item.old_price)} → ${formatPrice(item.price)}` })
      : h("span", { text: formatPrice(item.price) }),
    h("span", { text: relTime(item.created_at) }),
  ];

  return h(
    "article",
    { class: `notif${item.is_read ? "" : " unread"}` },
    thumb(item.image, "notif-thumb"),
    h(
      "div",
      { class: "notif-body" },
      h("div", { class: "notif-title" }, h("a", { href: item.url, target: "_blank", rel: "noopener", text: item.title })),
      h("div", { class: "notif-meta" }, meta)
    ),
    h(
      "div",
      { class: "notif-actions" },
      item.is_read
        ? null
        : h("button", {
            class: "btn btn-sm",
            text: "Прочитано",
            onclick: async () => {
              try {
                const data = await api("/api/notifications/read", {
                  method: "POST",
                  body: JSON.stringify({ ids: [item.id], source_id: state.activeSourceId }),
                });
                item.is_read = true;
                if (state.stats) state.stats.unread_notifications = data.unread;
                applyStats({ ...(state.stats || {}), unread_notifications: data.unread });
                renderNotifications();
                loadSources();
              } catch (error) {
                toast({ title: "Ошибка", text: error.message, kind: "error" });
              }
            },
          })
    )
  );
}

function renderNotifications() {
  $("#notif-list").replaceChildren(...state.notifs.map(notifNode));
  $("#notif-empty").classList.toggle("hidden", state.notifs.length > 0);
  $("#notif-more-wrap").classList.toggle("hidden", state.notifs.length >= state.notifsTotal);
}

async function loadScans() {
  if (state.activeSourceId === null) return;
  try {
    state.scans = await api(`/api/scans?source_id=${state.activeSourceId}&limit=30`);
    renderScans();
  } catch (error) {
    toast({ title: "Ошибка журнала", text: error.message, kind: "error" });
  }
}

function renderScans() {
  $("#log-body").replaceChildren(
    ...state.scans.map((row) =>
      h(
        "tr",
        {},
        h("td", { text: String(row.id) }),
        h("td", { text: formatDateTime(row.started_at) }),
        h(
          "td",
          {},
          h("span", {
            class: `status-${row.status}`,
            text: row.status === "ok" ? "успех" : row.status === "error" ? "ошибка" : "выполняется",
          })
        ),
        h("td", { text: String(row.items_found) }),
        h("td", { text: String(row.new_items) }),
        h("td", { text: String(row.price_changes) }),
        h("td", { class: "msg", text: row.message || "—" })
      )
    )
  );
  $("#log-empty").classList.toggle("hidden", state.scans.length > 0);
}

async function loadGlobalSettings() {
  try {
    const data = await api("/api/settings");
    $("#set-interval").value = String(Math.max(1, Math.round(data.poll_interval / 60)));
  } catch (error) {
    toast({ title: "Ошибка настроек", text: error.message, kind: "error" });
  }
}

function fillSourceSettings() {
  const source = activeSource();
  const form = $("#source-settings-form");
  if (!source) {
    form.classList.add("hidden");
    return;
  }
  form.classList.remove("hidden");
  $("#set-name").value = source.name;
  $("#set-url").value = source.url;
  $("#set-city").value = source.city_name || "";
  $("#set-enabled").checked = !!source.is_enabled;
  $("#set-sort").checked = !!source.sort_by_date;
}

function updateNotifyState() {
  const support = "Notification" in window;
  const text = !support
    ? "Браузер не поддерживает уведомления"
    : Notification.permission === "granted"
      ? "Уведомления включены"
      : Notification.permission === "denied"
        ? "Уведомления заблокированы в настройках браузера"
        : "Уведомления выключены";
  $("#notify-state").textContent = text;
  $("#notify-enable").disabled = !support || Notification.permission !== "default";
}

function showSystemNotification(item) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const body =
    item.type === "price_change" && item.old_price !== null
      ? `${formatPrice(item.old_price)} → ${formatPrice(item.price)}`
      : formatPrice(item.price);
  try {
    const note = new Notification("Avito Tracker", { body: `${item.title}\n${body}`, icon: item.image || undefined });
    note.onclick = () => { window.focus(); note.close(); };
  } catch (_) {}
}

function addNotification(item) {
  state.notifs.unshift(item);
  if (state.notifs.length > 80) state.notifs.pop();
  state.notifsTotal += 1;
  if (state.tab === "notifications") renderNotifications();
  const title = item.type === "price_change" ? "Изменилась цена" : "Новое объявление";
  const text =
    item.type === "price_change" && item.old_price !== null
      ? `${item.title}: ${formatPrice(item.old_price)} → ${formatPrice(item.price)}`
      : `${item.title}: ${formatPrice(item.price)}`;
  toast({ title, text, image: item.image, onClick: () => window.open(item.url, "_blank") });
  showSystemNotification(item);
}

async function refreshActiveSource(silent = false) {
  if (state.activeSourceId === null) {
    renderListings();
    return;
  }
  if (state.tab === "listings" || state.tab === "notifications") {
    await loadListings(true);
    await loadNotifications(true);
  } else {
    await loadListings(true);
  }
  await loadScans();
  await loadStats();
  await loadSources();
  state.lastRefresh = Date.now();
  renderUpdated();
  if (!silent) renderListings();
}

async function liveUpdate({ full = false } = {}) {
  if (state.activeSourceId === null) return;
  if (full) await loadListings(true);
  else await syncListings();
  if (state.tab === "notifications") await loadNotifications(true);
  await loadStats();
  state.lastRefresh = Date.now();
  renderUpdated();
}

async function liveTick() {
  if (document.hidden || state.scanning || state.activeSourceId === null) return;
  await liveUpdate();
  if (!state.wsOpen) {
    if (state.tab === "notifications") await loadNotifications(true);
    await loadSources();
  }
}

async function runScan(sourceId = state.activeSourceId) {
  if (state.scanning) return;
  state.scanning = true;
  state.scanProgress = "Сканирую…";
  $("#scan-btn").classList.add("busy");
  $("#scan-btn").disabled = true;
  renderStatus();
  try {
    const data = await api("/api/scan", { method: "POST", body: JSON.stringify({ source_id: sourceId }) });
    const results = data.results || [];
    if (!results.length) {
      toast({ title: "Нет активных списков", text: "Добавьте список или включите сканирование", kind: "error" });
    }
    for (const result of results) {
      if (result.status === "error") {
        toast({ title: "Ошибка скана", text: result.message || "неизвестно", kind: "error" });
      }
    }
    const ok = results.filter((result) => result.status === "ok");
    if (ok.length) {
      const found = ok.reduce((sum, result) => sum + result.items_found, 0);
      const created = ok.reduce((sum, result) => sum + result.new_items, 0);
      const changed = ok.reduce((sum, result) => sum + result.price_changes, 0);
      toast({ title: "Скан завершён", text: `Найдено ${found}, новых ${created}, смена цены ${changed}` });
    }
    await liveUpdate();
  } catch (error) {
    if (error.message !== "скан уже выполняется") {
      toast({ title: "Ошибка скана", text: error.message, kind: "error" });
    }
  } finally {
    state.scanning = false;
    state.scanProgress = null;
    $("#scan-btn").classList.remove("busy");
    $("#scan-btn").disabled = false;
    renderStatus();
  }
}

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${name}`));
  if (name === "notifications") loadNotifications(state.notifs.length === 0);
  if (name === "log") loadScans();
  if (name === "settings") { fillSourceSettings(); loadGlobalSettings(); }
}

function renderUpdated() {
  const label = $("#updated-label");
  if (!state.lastRefresh) {
    label.textContent = "—";
    return;
  }
  const seconds = Math.max(0, Math.round((Date.now() - state.lastRefresh) / 1000));
  label.textContent = seconds < 5 ? "обновлено только что" : `обновлено ${seconds} с назад`;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = socket;

  socket.addEventListener("open", () => {
    state.wsOpen = true;
    state.reconnectDelay = 1000;
    renderStatus();
  });

  socket.addEventListener("message", async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (message.type === "hello" || message.type === "stats") {
      if (!state.stats) applyStats(message.stats);
    } else if (message.type === "scan_status") {
      state.scanning = message.state === "running";
      state.scanProgress =
        message.state === "running" && message.total > 1
          ? `Сканирую ${message.current}/${message.total}…`
          : state.scanning
            ? "Сканирую…"
            : null;
      $("#scan-btn").disabled = state.scanning;
      renderStatus();
    } else if (message.type === "notification") {
      const isActive = message.source_id === state.activeSourceId;
      if (state.stats && isActive) {
        applyStats({ ...state.stats, unread_notifications: (state.stats.unread_notifications || 0) + 1 });
      }
      if (isActive) addNotification(message.notification);
      loadSources();
    } else if (message.type === "scan_finished") {
      state.scanning = false;
      state.scanProgress = null;
      $("#scan-btn").disabled = false;
      const isActive = message.source_id === state.activeSourceId;
      const result = message.result || {};
      if (isActive) applyStats(message.stats);
      await loadSources();
      if (isActive) {
        await liveUpdate();
        await loadScans();
      }
      if (result.status === "error" && isActive) {
        toast({ title: "Ошибка скана", text: result.message || "неизвестно", kind: "error" });
      }
      renderStatus();
    }
  });

  socket.addEventListener("close", () => {
    state.wsOpen = false;
    state.ws = null;
    state.reconnectDelay = Math.min(state.reconnectDelay * 1.7, 15000);
    setTimeout(connectWS, state.reconnectDelay);
  });

  socket.addEventListener("error", () => socket.close());
  setInterval(() => {
    if (socket.readyState === WebSocket.OPEN) socket.send("ping");
  }, 30000);
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
  $("#bell-btn").addEventListener("click", () => switchTab("notifications"));
  $("#scan-btn").addEventListener("click", () => runScan());

  document.querySelectorAll("#view-switch button").forEach((button) => {
    button.addEventListener("click", () => {
      state.view = button.dataset.view;
      localStorage.setItem("avito.view", state.view);
      renderViewSwitch();
      renderListings();
    });
  });

  let searchTimer = null;
  $("#search-input").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = event.target.value.trim();
      loadListings(true);
    }, 300);
  });

  $("#sort-select").addEventListener("change", (event) => applySort(event.target.value));
  $("#fresh-select").addEventListener("change", (event) => {
    state.fresh = event.target.value;
    localStorage.setItem("avito.fresh", state.fresh);
    loadListings(true);
  });
  $("#only-active").addEventListener("change", (event) => {
    state.active = event.target.checked;
    loadListings(true);
  });
  $("#only-unread").addEventListener("change", (event) => {
    state.unreadOnly = event.target.checked;
    loadNotifications(true);
  });

  $("#listings-more").addEventListener("click", () => loadListings(false));
  $("#notif-more").addEventListener("click", () => loadNotifications(false));

  $("#read-all-btn").addEventListener("click", async () => {
    try {
      const data = await api("/api/notifications/read", {
        method: "POST",
        body: JSON.stringify({ ids: null, source_id: state.activeSourceId }),
      });
      applyStats({ ...(state.stats || {}), unread_notifications: data.unread });
      await loadSources();
      await loadNotifications(true);
    } catch (error) {
      toast({ title: "Ошибка", text: error.message, kind: "error" });
    }
  });

  $("#add-source-btn").addEventListener("click", () => {
    const form = $("#source-form");
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) $("#src-url").focus();
  });
  $("#src-cancel").addEventListener("click", () => $("#source-form").classList.add("hidden"));

  $("#source-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const source = await api("/api/sources", {
        method: "POST",
        body: JSON.stringify({
          url: $("#src-url").value.trim(),
          name: $("#src-name").value.trim(),
          city_name: $("#src-city").value.trim(),
        }),
      });
      $("#source-form").classList.add("hidden");
      $("#src-url").value = "";
      $("#src-name").value = "";
      $("#src-city").value = "";
      state.activeSourceId = source.id;
      localStorage.setItem("avito.source", String(source.id));
      await loadSources();
      fillSourceSettings();
      await refreshActiveSource(true);
      toast({ title: "Список добавлен", text: `${source.name} — запускаю первый скан` });
      runScan(source.id);
    } catch (error) {
      toast({ title: "Не удалось добавить", text: error.message, kind: "error" });
    }
  });

  $("#source-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const source = activeSource();
    if (!source) return;
    try {
      await api(`/api/sources/${source.id}`, {
        method: "PUT",
        body: JSON.stringify({
          name: $("#set-name").value.trim(),
          url: $("#set-url").value.trim(),
          city_name: $("#set-city").value.trim(),
          is_enabled: $("#set-enabled").checked,
          sort_by_date: $("#set-sort").checked,
        }),
      });
      $("#source-msg").textContent = "Сохранено";
      $("#source-msg").style.color = "var(--accent-hover)";
      await loadSources();
      fillSourceSettings();
      setTimeout(() => { $("#source-msg").textContent = ""; }, 3000);
    } catch (error) {
      $("#source-msg").textContent = error.message;
      $("#source-msg").style.color = "var(--danger)";
    }
  });

  $("#delete-source-btn").addEventListener("click", async () => {
    const source = activeSource();
    if (!source) return;
    if (!confirm(`Удалить список «${source.name}»? Все его объявления и уведомления будут удалены.`)) return;
    try {
      await api(`/api/sources/${source.id}`, { method: "DELETE" });
      localStorage.removeItem("avito.source");
      state.activeSourceId = null;
      await loadSources();
      fillSourceSettings();
      await refreshActiveSource(true);
      toast({ title: "Список удалён", text: source.name });
    } catch (error) {
      toast({ title: "Ошибка", text: error.message, kind: "error" });
    }
  });

  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("#settings-msg");
    const minutes = Math.max(1, Math.round(Number($("#set-interval").value)));
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ poll_interval: minutes * 60 }) });
      message.textContent = "Сохранено";
      message.style.color = "var(--accent-hover)";
      await loadStats();
      setTimeout(() => { message.textContent = ""; }, 3000);
    } catch (error) {
      message.textContent = error.message;
      message.style.color = "var(--danger)";
    }
  });

  $("#notify-enable").addEventListener("click", async () => {
    if (!("Notification" in window)) return;
    await Notification.requestPermission();
    updateNotifyState();
  });

  $("#new-banner-btn").addEventListener("click", () => {
    const mode = $("#new-banner").dataset.mode;
    hideNewBanner();
    if (mode === "top") window.scrollTo({ top: 0, behavior: "smooth" });
    else loadListings(true);
  });
  $("#new-banner-close").addEventListener("click", hideNewBanner);

  setInterval(() => {
    renderStatus();
    renderUpdated();
  }, 1000);

  setInterval(liveTick, 45000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) liveTick();
  });
}

async function init() {
  bindEvents();
  renderViewSwitch();
  updateNotifyState();
  $("#fresh-select").value = state.fresh;
  $("#sort-select").value = state.sort;
  connectWS();
  await loadSources();
  fillSourceSettings();
  await loadGlobalSettings();
  await refreshActiveSource();
}

document.addEventListener("DOMContentLoaded", init);

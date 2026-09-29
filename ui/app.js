"use strict";

const $ = (s) => document.querySelector(s);

let STATE = null;
let FILTER = "all";
let HIDE_DONE = false;
let failures = 0;
const seenReady = new Set();
let firstLoad = true;

/* ---------- Вспомогательное ---------- */

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function dur(sec) {
  if (sec == null || sec < 0) return "—";
  sec = Math.floor(sec);
  if (sec < 60) return sec + " с";
  const m = Math.floor(sec / 60);
  if (m < 60) return m + " мин";
  const h = Math.floor(m / 60);
  if (h < 24) return h + " ч " + String(m % 60).padStart(2, "0") + " мин";
  const d = Math.floor(h / 24);
  return d + " д " + String(h % 24).padStart(2, "0") + " ч";
}

function toast(title, msg, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.innerHTML = "<span class='t'>" + esc(title) + "</span>" +
    (msg ? "<span class='m'>" + esc(msg) + "</span>" : "");
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 300);
  }, 5200);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

function showBanner(text) {
  let el = document.getElementById("banner");
  if (!el) {
    el = document.createElement("div");
    el.id = "banner";
    el.className = "banner";
    document.body.prepend(el);
  }
  el.textContent = text;
  el.hidden = false;
}

function hideBanner() {
  const el = document.getElementById("banner");
  if (el) el.hidden = true;
}

/* ---------- Отрисовка ---------- */

function renderCards(s) {
  const st = s.stats;
  const cards = [
    { k: "Кампаний", v: st.campaigns, cls: "" },
    { k: "Дропсов", v: st.drops_total, cls: "" },
    { k: "Готовы к забору", v: st.drops_ready, cls: st.drops_ready ? "good" : "" },
    { k: "Забрано всего", v: st.claimed, cls: "accent" },
    { k: "Общий прогресс", v: st.avg_percent + "%", cls: "" },
    { k: "Без привязки", v: st.unlinked, cls: st.unlinked ? "bad" : "" }
  ];
  $("#cards").innerHTML = cards.map(c =>
    '<div class="card ' + c.cls + '"><div class="k">' + c.k +
    '</div><div class="v">' + c.v + "</div></div>").join("");
}

function dropRow(d) {
  const pct = d.percent;
  const full = pct >= 100;
  const cls = d.claimed ? "done" : "";

  let right;
  if (d.claimed) {
    right = "<b>забрано</b>";
  } else if (d.ready) {
    right = "<span class='ready'>ГОТОВО</span><span class='eta'>ждёт клейма</span>";
  } else {
    right = "<b>" + d.watched_min + "</b>/" + d.needed_min + " мин" +
      "<span class='eta'>осталось " + esc(d.left) + (d.eta ? " " + esc(d.eta) : "") + "</span>";
  }

  let sub;
  if (d.blocked) {
    sub = "<span class='block'>сначала: " + esc((d.needs || []).join(", ") || "предыдущий") + "</span>";
  } else {
    sub = "<span class='need'>награда: " + esc(d.benefit) + "</span>";
  }

  return '<div class="drop ' + cls + '">' +
    '<div class="drop-name"><span class="n">' + esc(d.name) + "</span>" + sub + "</div>" +
    '<div class="bar' + (full ? " full" : "") + '"><i style="width:' + pct + '%"></i></div>' +
    '<div class="drop-right">' + right + "</div>" +
    "</div>";
}

function channelsBlock(c) {
  if (c.expired || !c.channels || !c.channels.length) return "";
  const shown = c.channels.slice(0, 10);
  let html = shown.map(ch =>
    '<a class="chan" href="' + esc(ch.url) + '" target="_blank" rel="noopener">' +
    esc(ch.name) + "</a>").join("");
  const more = c.channels_total - shown.length;
  if (more > 0) html += '<span class="more">и ещё ' + more + "</span>";
  return '<div class="channels"><span class="lbl">где смотреть</span>' + html + "</div>";
}

function campaignBlock(c) {
  const tags = [];
  if (c.expired) tags.push('<span class="tag">закончилась</span>');
  else if (c.deadline_seconds != null)
    tags.push('<span class="tag ' + (c.deadline_seconds < 3600 ? "warn" : "") +
      '">до конца ' + dur(c.deadline_seconds) + "</span>");

  if (!c.account_connected) {
    const link = c.account_link_url
      ? ' <a href="' + esc(c.account_link_url) + '" target="_blank" rel="noopener">привязать →</a>'
      : "";
    tags.push('<span class="tag bad">аккаунт не привязан' + link + "</span>");
  }
  const readyCount = c.drops.filter(d => d.ready).length;
  if (readyCount) tags.push('<span class="tag ok">готовы: ' + readyCount + "</span>");

  const cls = c.expired ? "expired"
    : !c.account_connected ? "unlinked"
    : readyCount ? "ready"
    : (c.risk ? "risk" : "");

  return '<section class="camp ' + cls + '">' +
    '<div class="camp-head"><div class="camp-title"><strong>' + esc(c.name) +
    '</strong> <span class="game">' + esc(c.game) + "</span></div>" +
    '<div class="tags">' + tags.join("") + "</div></div>" +
    '<div class="drops">' + c.drops.map(dropRow).join("") + "</div>" +
    channelsBlock(c) +
    "</section>";
}

function passes(c) {
  if (HIDE_DONE && c.drops.length && c.drops.every(d => d.claimed)) return false;
  switch (FILTER) {
    case "ready": return c.drops.some(d => d.ready);
    case "risk": return c.risk;
    case "unlinked": return !c.account_connected;
    default: return true;
  }
}

function renderCampaigns(s) {
  const list = (s.campaigns || []).filter(passes);
  const box = $("#campaigns");
  const empty = $("#empty");

  if (!list.length) {
    box.innerHTML = "";
    empty.hidden = false;
    empty.textContent = s.connected
      ? "Под текущий фильтр ничего не попало."
      : "Нет соединения с Twitch.";
    return;
  }
  empty.hidden = true;
  box.innerHTML = list.map(campaignBlock).join("");
}

function renderTop(s) {
  $("#account").textContent = s.viewer && s.viewer.login
    ? s.viewer.login + " · id " + s.viewer.user_id
    : "не авторизован";

  const conn = $("#chip-conn");
  const dot = conn.querySelector(".dot");
  const label = conn.querySelector("span");
  if (s.connected) {
    dot.className = "dot ok";
    label.textContent = "на связи";
  } else {
    dot.className = "dot bad";
    label.textContent = s.error ? "ошибка" : "нет связи";
  }

  $("#chip-rate").innerHTML = "темп <b>" +
    (s.rate > 0 ? (s.rate * 60).toFixed(1) + " мин/мин" : "—") + "</b>";

  const dl = s.deadline_seconds;
  $("#chip-deadline").innerHTML = "дедлайн <b>" +
    (dl != null && dl >= 0 ? dur(dl) : "—") + "</b>";

  $("#foot-left").textContent = s.error
    ? "Ошибка: " + s.error
    : "Обновлено в " + (s.updated_at || "—");
  $("#foot-right").textContent = "интервал " + s.poll_interval + " с";
}

/* ---------- Опрос сервера ---------- */

function notifyNewReady(s) {
  (s.campaigns || []).forEach(c => {
    c.drops.forEach(d => {
      if (!d.ready) return;
      const key = c.id + ":" + d.name;
      if (!seenReady.has(key)) {
        seenReady.add(key);
        if (!firstLoad) toast("Готово к забору", d.name, "ok");
      }
    });
  });
  firstLoad = false;
}

async function poll() {
  try {
    const s = await api("/api/state");
    failures = 0;
    STATE = s;
    renderTop(s);
    renderCards(s);
    renderCampaigns(s);
    notifyNewReady(s);
    hideBanner();
  } catch (e) {
    failures++;
    // Данные под шапкой остаются верными, поэтому не выбрасываем их:
    // помечаем потерю связи, но не затираем имя аккаунта.
    if (STATE) {
      showBanner("Связь с программой потеряна. Переподключение…");
    } else {
      $("#account").textContent = "сервер недоступен";
    }
    const dot = $("#chip-conn").querySelector(".dot");
    const label = $("#chip-conn").querySelector("span");
    dot.className = "dot bad";
    label.textContent = "нет связи";
  }
}

async function setAutoClaim(on) {
  try {
    await api("/api/autoclaim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!on })
    });
    toast(on ? "Автоклейм включён" : "Автоклейм выключен", "", on ? "ok" : "warn");
    poll();
  } catch (e) {
    toast("Не удалось переключить", String(e), "bad");
  }
}

/* ---------- Запуск ---------- */

document.addEventListener("DOMContentLoaded", () => {
  $("#auto-claim").addEventListener("change", (e) => setAutoClaim(e.target.checked));

  $("#btn-refresh").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.classList.add("busy");
    btn.textContent = "Обновляю…";
    try {
      const r = await api("/api/refresh", { method: "POST" });
      if (r.claims && r.claims.length) {
        r.claims.forEach(n => toast("Забрано", n, "ok"));
      } else {
        toast("Обновлено", "новых наград нет", "ok");
      }
    } catch (err) {
      toast("Ошибка", String(err), "bad");
    }
    btn.classList.remove("busy");
    btn.textContent = "Обновить";
    poll();
  });

  document.querySelectorAll(".tab").forEach(t => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(x => x.classList.remove("is-active"));
      t.classList.add("is-active");
      FILTER = t.dataset.filter;
      if (STATE) renderCampaigns(STATE);
    });
  });

  $("#hide-done").addEventListener("change", (e) => {
    HIDE_DONE = e.target.checked;
    if (STATE) renderCampaigns(STATE);
  });

  poll();
  setInterval(poll, 3000);
});

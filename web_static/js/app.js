/* BUII dashboard client. Externalized so the dashboard can enforce a strict CSP. */

let currentUser = null;
let currentGuilds = [];
let currentGuildId = localStorage.getItem("buii-last-guild") || null;
let allCommands = [];
const PAGE_SIZE = 5;
const dashboardState = {
  recentRows: [], recentPage: 1,
  leaderboardRows: [], leaderboardPage: 1,
  musicRows: [], musicPage: 1,
  musicLogRows: [], musicLogPage: 1,
};

function initials(name) {
  return (name || "?").split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();
}

function valueOrNull(id) {
  const v = document.getElementById(id).value;
  return v === "" ? null : v;
}

function fmtNum(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString();
}

function updatedLabel() {
  return "Updated " + new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function markUpdated(id) {
  const element = document.getElementById(id);
  if (element) element.textContent = updatedLabel();
}

function renderPager(id, page, totalPages, onChange) {
  const pager = document.getElementById(id);
  if (!pager) return;
  pager.replaceChildren();
  if (totalPages <= 1) return;
  for (const label of ["‹", ...Array.from({length: totalPages}, (_, i) => String(i + 1)), "›"]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    const target = label === "‹" ? page - 1 : label === "›" ? page + 1 : Number(label);
    button.disabled = target < 1 || target > totalPages || target === page;
    if (target === page) button.classList.add("selected");
    button.addEventListener("click", () => onChange(target));
    pager.appendChild(button);
  }
}

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeDiscordImageURL(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "cdn.discordapp.com" ? url.href : null;
  } catch {
    return null;
  }
}

function setDiscordBackground(element, url) {
  const safe = safeDiscordImageURL(url);
  element.style.backgroundImage = safe ? `url("${safe}")` : "";
  element.style.backgroundSize = safe ? "cover" : "";
}

async function apiFetch(url, options = {}) {
  const res = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: { ...(options.headers || {}), "Accept": "application/json" },
  });

  if (res.status === 401) {
    showLoginGate("Your dashboard session expired. Please log in again.");
    throw new Error("Session expired");
  }

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (data.error) message = data.error;
    } catch (_) { /* non-JSON error response */ }
    throw new Error(message);
  }
  return res;
}

async function boot() {
  try {
    const res = await apiFetch("/api/me");
    const data = await res.json();
    currentUser = data.user;
    currentGuilds = data.guilds || [];
    showApp();
  } catch (e) {
    if (e.message !== "Session expired") showLoginGate("Couldn't reach the dashboard API. Is the bot running?");
  }
}

function showLoginGate(errorText) {
  document.getElementById("app").style.display = "none";
  document.getElementById("loginGate").style.display = "flex";
  const el = document.getElementById("loginError");
  const params = new URLSearchParams(location.search);
  const loginError = params.get("login_error");
  if (loginError || errorText) {
    el.textContent = loginError ? `Login failed: ${loginError}` : errorText;
    el.style.display = "block";
  }
}

function showApp() {
  document.getElementById("loginGate").style.display = "none";
  document.getElementById("app").style.display = "";
  document.getElementById("profileName").textContent = currentUser.username || "Discord user";
  document.getElementById("profileNameLarge").textContent = currentUser.username || "Discord user";

  for (const id of ["profileAvatar", "profileAvatarLarge"]) {
    const el = document.getElementById(id);
    const safeAvatar = safeDiscordImageURL(currentUser.avatar_url);
    if (safeAvatar) {
      setDiscordBackground(el, safeAvatar);
      el.textContent = "";
    } else {
      el.textContent = initials(currentUser.username);
    }
  }

  renderServerList();
  loadCommands().catch(showLoadError);

  if (!currentGuilds.find(g => g.id === currentGuildId)) {
    currentGuildId = currentGuilds.length ? currentGuilds[0].id : null;
  }
  if (currentGuildId) selectServer(currentGuildId);
  else {
    const list = document.getElementById("serverList");
    list.textContent = "No manageable servers found — you need Manage Server permission in a server BUII is also in.";
  }

  showPage(location.hash.replace("#", "") || "home");
}

function renderServerList() {
  const list = document.getElementById("serverList");
  list.replaceChildren();
  if (!currentGuilds.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.style.padding = "12px";
    empty.textContent = "No servers yet.";
    list.appendChild(empty);
    return;
  }

  for (const guild of currentGuilds) {
    const server = document.createElement("div");
    server.className = `server${guild.id === currentGuildId ? " active" : ""}`;
    server.dataset.guild = guild.id;
    server.tabIndex = 0;
    server.setAttribute("role", "button");
    server.setAttribute("aria-label", `Select ${guild.name}`);
    server.addEventListener("click", () => selectServer(guild.id));
    server.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectServer(guild.id);
      }
    });

    const icon = document.createElement("div");
    icon.className = "server-icon";
    if (guild.icon_url) setDiscordBackground(icon, guild.icon_url);
    if (!safeDiscordImageURL(guild.icon_url)) icon.textContent = initials(guild.name);

    const name = document.createElement("div");
    const bold = document.createElement("b");
    bold.textContent = guild.name || "Unnamed server";
    name.appendChild(bold);
    server.append(icon, name);
    list.appendChild(server);
  }
}

function selectServer(id) {
  if (!currentGuilds.some(g => g.id === id)) {
    toast("That server is no longer available.");
    return;
  }
  currentGuildId = id;
  localStorage.setItem("buii-last-guild", id);
  applyPanelPrefs();
  document.querySelectorAll(".server").forEach(el => el.classList.toggle("active", el.dataset.guild === id));
  const g = currentGuilds.find(guild => guild.id === id);
  if (g) {
    document.getElementById("heroServer").textContent = g.name;
    const icon = document.getElementById("heroServerIcon");
    if (safeDiscordImageURL(g.icon_url)) {
      setDiscordBackground(icon, g.icon_url);
      icon.textContent = "";
    } else {
      icon.style.backgroundImage = "";
      icon.textContent = initials(g.name);
    }
  }

  Promise.allSettled([
    loadOverview(id),
    loadGrowthChart(id),
    loadAnalytics(id),
    loadLeaderboard(id),
    loadMusicLeaderboard(id),
    loadMusicLog(id),
    loadMusicInsights(id),
    loadConfig(id),
    loadRecentJoins(id),
  ]);
  toast((g ? g.name : "Server") + " selected");
}

function showLoadError(error) {
  if (error?.message && error.message !== "Session expired") toast(error.message);
}

async function loadOverview(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/overview`)).json();
    document.getElementById("statMembers").textContent = fmtNum(d.guild && d.guild.member_count);
    document.getElementById("statJoins24h").textContent = fmtNum(d.joins_24h);
    document.getElementById("statJoinsTotal").textContent = fmtNum(d.total_joins) + " all-time";
    document.getElementById("statRisk").textContent = fmtNum(d.extreme_risk_count);
    document.getElementById("statLatency").textContent = d.bot_latency_ms !== null ? d.bot_latency_ms + "ms" : "—";
    document.getElementById("anaMembers").textContent = fmtNum(d.guild && d.guild.member_count);
    document.getElementById("anaTotalJoins").textContent = fmtNum(d.total_joins);
    document.getElementById("anaJoins24h").textContent = fmtNum(d.joins_24h);
    document.getElementById("anaRisk").textContent = fmtNum(d.extreme_risk_count);
    document.getElementById("anaRiskRate").textContent = d.total_joins ? ((Number(d.extreme_risk_count || 0) / Number(d.total_joins)) * 100).toFixed(1) + "%" : "—";
    document.getElementById("anaFreshness").textContent = "Just now";
    document.getElementById("healthStatus").textContent = "Connected";
    document.getElementById("healthTrend").textContent = "Operational";
    document.getElementById("healthLatency").textContent = d.bot_latency_ms !== null ? d.bot_latency_ms + "ms" : "—";
    document.getElementById("healthTopCode").textContent = d.top_invite_codes?.[0]
      ? `${d.top_invite_codes[0].code} (${d.top_invite_codes[0].uses})` : "—";
  } catch (e) { showLoadError(e); }
}

async function loadGrowthChart(guildId) {
  if (!guildId) return;
  try {
    const days = document.getElementById("growthPeriod").value || 30;
    const d = await (await apiFetch(`/api/guilds/${guildId}/growth?days=${encodeURIComponent(days)}`)).json();
    const series = d.days || [];
    const max = Math.max(1, ...series.map(x => Number(x.joins) || 0));
    const total = series.reduce((a, x) => a + (Number(x.joins) || 0), 0);
    document.getElementById("growthTotal").textContent = "+" + fmtNum(total);
    document.getElementById("growthSubtitle").textContent = `Last ${days} days`;

    const chart = document.getElementById("growthChart");
    chart.replaceChildren();
    for (const point of series) {
      const bar = document.createElement("span");
      const joins = Number(point.joins) || 0;
      bar.style.height = `${Math.max(4, Math.round(joins / max * 100))}%`;
      bar.title = `${point.date}: ${joins} joins`;
      chart.appendChild(bar);
    }

    const labels = document.getElementById("growthLabels");
    labels.replaceChildren();
    if (series.length) {
      const fmt = s => new Date(s).toLocaleDateString(undefined, { month: "short", day: "numeric" });
      for (const date of [series[0].date, series[Math.floor(series.length / 2)].date, series[series.length - 1].date]) {
        const span = document.createElement("span");
        span.textContent = fmt(date);
        labels.appendChild(span);
      }
    }
    const dailyAverage = series.length ? total / series.length : 0;
    const peak = series.reduce((best, point) => Number(point.joins) > Number(best.joins) ? point : best, { joins: 0, date: null });
    document.getElementById("anaDailyAvg").textContent = dailyAverage.toFixed(1);
    document.getElementById("anaPeakDay").textContent = peak.date ? new Date(peak.date).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—";
    document.getElementById("anaPeakJoins").textContent = peak.date ? `${peak.joins} joins on peak day` : "No growth data";

  } catch (e) { showLoadError(e); }
}


async function loadAnalytics(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/analytics`)).json();
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
    const n = value => fmtNum(Number(value) || 0);
    const growth = d.growth || {};
    const retention = d.retention || {};
    const risk = d.risk || {};
    const queue = d.queue || {};
    const ratings = d.ratings || {};
    const telemetry = d.telemetry || {};
    const syncSummary = d.sync_summary || {};
    const playlistVisible = Boolean(d.spotify_premium);
    ["playlistSyncCard", "playlistSyncFailuresCard", "healthSyncRow", "healthSyncQueueRow"].forEach(id => {
      const element = document.getElementById(id);
      if (element) element.hidden = !playlistVisible;
    });

    set("analyticsTrackedInvites", n(growth.tracked_invites));
    set("analyticsCurrentJoins", n(growth.current_joins));
    set("analyticsRetained7d", n(retention.retained_joins));
    set("analyticsMatureJoins", n(retention.mature_joins));
    set("analyticsReturningMembers", n(retention.returning_members));

    const current = Number(growth.current_joins) || 0;
    const previous = Number(growth.previous_joins) || 0;
    const change = previous ? ((current - previous) / previous) * 100 : null;
    set("analyticsJoinChange", change === null ? "New data" : `${change >= 0 ? "+" : ""}${change.toFixed(1)}%`);
    set("analyticsCurrentPeriod", n(current));
    set("analyticsBestPeriod", current >= previous ? "Current" : "Previous");

    set("analyticsNewAccountRisk", n(risk.new_account));
    set("analyticsAmbiguousInvites", n(risk.ambiguous_invites));
    set("analyticsOpenSongs", n(queue.open_songs));
    set("analyticsAwaitingPreview", n(queue.awaiting_preview));

    const total = Number(ratings.total) || 0;
    const buckets = [
      ["Low", ratings.low, "ratingBarLow", "ratingCountLow"],
      ["Below", ratings.below_average, "ratingBarBelow", "ratingCountBelow"],
      ["Average", ratings.average, "ratingBarAverage", "ratingCountAverage"],
      ["Good", ratings.good, "ratingBarGood", "ratingCountGood"],
      ["Excellent", ratings.excellent, "ratingBarExcellent", "ratingCountExcellent"],
    ];
    buckets.forEach(([, count, barId, countId]) => {
      const amount = Number(count) || 0;
      const bar = document.getElementById(barId);
      if (bar) bar.style.width = `${total ? Math.round(amount / total * 100) : 0}%`;
      set(countId, n(amount));
    });

    const commandTotal = Number(telemetry.commands_today) || 0;
    const commandErrors = Number(telemetry.command_errors) || 0;
    set("analyticsCommandsToday", n(commandTotal));
    set("analyticsMostUsedCommand", telemetry.most_used_command ? `/${telemetry.most_used_command}` : "—");
    set("analyticsCommandErrorRate", commandTotal ? `${(commandErrors / commandTotal * 100).toFixed(1)}%` : "0%");
    set("analyticsSyncFailures", n(telemetry.sync_failures));
    set("analyticsSyncAttempts", n(telemetry.sync_attempts));
    set("analyticsCatalogSongs", n(syncSummary.catalog_songs));
    set("analyticsMarkedSynced", n(syncSummary.marked_synced));
    set("analyticsPlaylistTracks", n(syncSummary.playlist_track_count));
    set("analyticsMatchedSongs", n(syncSummary.matched_song_count));
    const eventBox = document.getElementById("analyticsAdminEvents");
    if (eventBox) {
      eventBox.replaceChildren();
      const events = telemetry.admin_events || [];
      if (!events.length) {
        const empty = document.createElement("div");
        empty.className = "empty-state compact";
        empty.innerHTML = "<strong>No admin events yet</strong><span>Configuration changes will appear here as they happen.</span>";
        eventBox.appendChild(empty);
      } else {
        events.forEach(event => {
          const row = document.createElement("div");
          row.className = "metric-placeholder";
          const when = new Date(event.created_at).toLocaleString();
          row.innerHTML = `<span>${escapeHTML(event.details || event.name)}</span><strong>${escapeHTML(when)}</strong>`;
          eventBox.appendChild(row);
        });
      }
    }
  } catch (e) { showLoadError(e); }
}

function rankingRows(items, labelKey, valueKey, formatValue, page = 1) {
  const max = Math.max(1, ...items.map(x => Number(x[valueKey]) || 0));
  const start = (page - 1) * PAGE_SIZE;
  return items.slice(start, start + PAGE_SIZE).map((x, i) => {
    const value = Number(x[valueKey]) || 0;
    const label = escapeHTML(x[labelKey]);
    const formatted = escapeHTML(formatValue ? formatValue(value) : fmtNum(value));
    return `<div class="ranking-row"><span>${String(start + i + 1).padStart(2, "0")}</span><strong>${label}</strong><div class="ranking-bar"><i style="width:${Math.round(value / max * 100)}%"></i></div><b>${formatted}</b></div>`;
  }).join("");
}

function setSafeHTML(element, html) {
  element.innerHTML = html;
}

async function loadLeaderboard(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/leaderboard`)).json();
    dashboardState.leaderboardRows = d.leaderboard || [];
    dashboardState.leaderboardPage = 1;
    const render = () => {
      const rows = dashboardState.leaderboardRows;
      const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
      const html = rows.length ? rankingRows(rows, "inviter", "joins", null, dashboardState.leaderboardPage) : '<div class="muted" style="padding:8px 0">No tracked invites yet.</div>';
      setSafeHTML(document.getElementById("homeLeaderboard"), html);
      setSafeHTML(document.getElementById("statsLeaderboard"), html);
      renderPager("homeLeaderboardPager", dashboardState.leaderboardPage, totalPages, page => { dashboardState.leaderboardPage = page; render(); });
      renderPager("statsLeaderboardPager", dashboardState.leaderboardPage, totalPages, page => { dashboardState.leaderboardPage = page; render(); });
    };
    render();
    markUpdated("homeLeaderboardUpdated");
    markUpdated("statsLeaderboardUpdated");
  } catch (e) { showLoadError(e); }
}

async function loadMusicLeaderboard(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/music-leaderboard`)).json();
    const rows = (d.songs || []).map(s => ({
      label: s.title + (s.artist ? " — " + s.artist : ""),
      avg_score: Number(s.avg_score) || 0,
    }));
    setSafeHTML(document.getElementById("musicLeaderboard"), rows.length
      ? rankingRows(rows, "label", "avg_score", v => v.toFixed(1) + "/10")
      : '<div class="muted" style="padding:8px 0">No rated songs with 2+ votes yet.</div>');
  } catch (e) { showLoadError(e); }
}

function renderRecentJoins() {
  const rows = dashboardState.recentRows;
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const start = (dashboardState.recentPage - 1) * PAGE_SIZE;
  const visibleRows = rows.slice(start, start + PAGE_SIZE);
  const notifications = document.getElementById("notificationList");
  const activity = document.getElementById("activityList");
  notifications.replaceChildren();
  activity.replaceChildren();
  document.getElementById("notificationCount").textContent = rows.length ? `${rows.length} recent` : "No recent joins";
  const newestJoin = rows[0] ? `${rows[0].join_date || ""}|${rows[0].user_name || ""}` : "";
  const seenJoin = localStorage.getItem("buii-notifications-seen") || "";
  document.getElementById("notificationDot").classList.toggle("show", Boolean(newestJoin && newestJoin !== seenJoin));
  if (!visibleRows.length) {
    const empty = document.createElement("div"); empty.className = "muted"; empty.style.padding = "12px"; empty.textContent = "No joins recorded yet.";
    notifications.appendChild(empty.cloneNode(true)); activity.appendChild(empty);
  } else {
    for (const join of visibleRows) {
      const notification = document.createElement("div"); notification.className = "notification"; notification.dataset.notification = "";
      const icon = document.createElement("div"); icon.className = "notification-icon"; icon.textContent = "✦";
      const content = document.createElement("div"); content.className = "notification-content";
      const strong = document.createElement("strong"); strong.textContent = join.user_name || "Unknown user";
      const invited = document.createElement("p"); invited.textContent = `Invited by ${join.inviter_name || "Unknown"}`;
      const date = document.createElement("span"); date.textContent = join.join_date || "";
      content.append(strong, invited, date); notification.append(icon, content); notifications.appendChild(notification);
      const row = document.createElement("div"); row.className = "activity-row";
      const aicon = document.createElement("div"); aicon.className = "activity-icon"; aicon.textContent = "✦";
      const info = document.createElement("div");
      const astrong = document.createElement("strong"); astrong.textContent = join.user_name || "Unknown user";
      const small = document.createElement("small"); small.textContent = `Invited by ${join.inviter_name || "Unknown"} · code ${join.invite_code || "Unknown"}`;
      const time = document.createElement("span"); time.className = "time"; time.textContent = join.join_date || "";
      info.append(astrong, small); row.append(aicon, info, time); activity.appendChild(row);
    }
  }
  document.getElementById("activityPageLabel").textContent = `Page ${dashboardState.recentPage} of ${totalPages}`;
  renderPager("activityPager", dashboardState.recentPage, totalPages, page => { dashboardState.recentPage = page; renderRecentJoins(); });
  markUpdated("recentJoinsUpdated");
}

async function loadRecentJoins(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/recent-joins?limit=25`)).json();
    dashboardState.recentRows = d.joins || [];
    dashboardState.recentPage = 1;
    renderRecentJoins();
  } catch (e) { showLoadError(e); }
}

function renderMusicLog() {
  const rows = dashboardState.musicLogRows;
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const start = (dashboardState.musicLogPage - 1) * PAGE_SIZE;
  const visible = rows.slice(start, start + PAGE_SIZE);
  const list = document.getElementById("musicLogList");
  list.replaceChildren();
  if (!visible.length) { const empty = document.createElement("div"); empty.className = "muted"; empty.textContent = "No songs have been logged yet."; list.appendChild(empty); }
  for (const song of visible) {
    const item = document.createElement("div"); item.className = "song-log-row";
    const number = document.createElement("span"); number.className = "song-log-number"; number.textContent = `#${song.song_number || "—"}`;
    const body = document.createElement("div"); body.className = "song-log-body";
    const title = document.createElement("strong"); title.textContent = song.title || "Untitled track";
    const meta = document.createElement("small"); meta.textContent = `${song.artist || "Unknown artist"} · requested by ${song.requested_by || "Unknown"}`;
    body.append(title, meta);
    const right = document.createElement("div"); right.className = "song-log-right";
    const status = document.createElement("span"); status.className = "song-status " + (song.status === "open" ? "open" : song.status === "closed" ? "closed" : "synced"); status.textContent = song.status;
    const time = document.createElement("small"); time.textContent = song.created_at ? new Date(song.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—";
    right.append(status, time); item.append(number, body, right); list.appendChild(item);
  }
  document.getElementById("musicLogPageLabel").textContent = `Page ${dashboardState.musicLogPage} of ${totalPages}`;
  renderPager("musicLogPager", dashboardState.musicLogPage, totalPages, page => { dashboardState.musicLogPage = page; renderMusicLog(); });
  markUpdated("musicLogUpdated");
}

async function loadMusicLog(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/music-log?limit=50`)).json();
    dashboardState.musicLogRows = d.songs || []; dashboardState.musicLogPage = 1; renderMusicLog();
  } catch (e) { showLoadError(e); }
}

async function loadMusicInsights(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/music-insights`)).json();
    const list = document.getElementById("requesterList");
    list.replaceChildren();
    const requesters = d.requesters || [];
    if (!requesters.length) { const empty = document.createElement("div"); empty.className = "muted"; empty.textContent = "No song requests yet."; list.appendChild(empty); }
    requesters.forEach((requester, index) => {
      const row = document.createElement("div"); row.className = "requester-row";
      const rank = document.createElement("span"); rank.className = "requester-rank"; rank.textContent = String(index + 1).padStart(2, "0");
      const name = document.createElement("strong"); name.textContent = requester.name || "Unknown";
      const count = document.createElement("span"); count.className = "requester-count"; count.textContent = `${requester.songs} ${requester.songs === 1 ? "song" : "songs"}`;
      row.append(rank, name, count); list.appendChild(row);
    });
    markUpdated("requestersUpdated");
    const sync = d.sync || {};
    const playlistVisible = Boolean(d.spotify_premium);
    ["healthSyncRow", "healthSyncQueueRow"].forEach(id => {
      const element = document.getElementById(id);
      if (element) element.hidden = !playlistVisible;
    });
    const syncText = !sync.playlist_configured ? "Not configured" : `${sync.synced}/${sync.total} synced`;
    document.getElementById("healthSync").textContent = syncText;
    document.getElementById("healthSyncQueue").textContent = sync.playlist_configured ? `${sync.pending} pending` : "Playlist unavailable";
  } catch (e) { showLoadError(e); }
}

async function loadConfig(guildId) {
  if (!guildId) return;
  try {
    const d = await (await apiFetch(`/api/guilds/${guildId}/config`)).json();
    document.getElementById("cfgPrefix").value = d.prefix || "";

    const fillSelect = (id, options, current) => {
      const sel = document.getElementById(id);
      sel.replaceChildren();
      const placeholder = document.createElement("option");
      placeholder.value = ""; placeholder.textContent = "— none —"; sel.appendChild(placeholder);
      for (const option of options) {
        const opt = document.createElement("option");
        opt.value = option.id; opt.textContent = option.name;
        if (current && current.id === option.id) opt.selected = true;
        sel.appendChild(opt);
      }
      enhancedSelects.get(id)?.();
    };

    fillSelect("cfgLogChannel", d.channels || [], d.log_channel);
    fillSelect("cfgMusicChannel", d.channels || [], d.music_channel);
    fillSelect("cfgAlertRole", d.roles || [], d.alert_role);
    fillSelect("cfgModRole", d.roles || [], d.mod_role);
    fillSelect("cfgMusicRole", d.roles || [], d.music_role);
    document.getElementById("cfgLockSeconds").value = d.music_lock_seconds || 0;
    document.getElementById("cfgLogStatus").textContent = d.log_channel ? `#${d.log_channel.name}` : "Not set (falls back to #welcome)";
    document.getElementById("cfgAlertStatus").textContent = d.alert_role ? d.alert_role.name : "Not set";
    document.getElementById("cfgModStatus").textContent = d.mod_role ? d.mod_role.name : "Not set";
    document.getElementById("cfgMusicChanStatus").textContent = d.music_channel ? `#${d.music_channel.name}` : "Any channel";
    document.getElementById("cfgMusicRoleStatus").textContent = d.music_role ? d.music_role.name : "Not set";
    document.getElementById("cfgLockStatus").textContent = d.music_lock_seconds ? `${d.music_lock_seconds}s lock` : "Disabled";
  } catch (e) { showLoadError(e); }
}

async function saveConfigField(field, value) {
  if (!currentGuildId) return;
  try {
    await apiFetch(`/api/guilds/${currentGuildId}/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    toast("Saved");
    await loadConfig(currentGuildId);
  } catch (e) { showLoadError(e); }
}

async function loadCommands() {
  const d = await (await apiFetch("/api/commands")).json();
  allCommands = d.commands || [];
  renderCommands(allCommands);
}

function renderCommands(commands) {
  const symbol = { music: "♪", growth: "↗", staff: "◈", admin: "⚙" };
  const list = document.getElementById("commandList");
  list.replaceChildren();
  for (const command of commands) {
    const card = document.createElement("div"); card.className = "command-card";
    card.dataset.name = command.name || ""; card.dataset.category = command.category || "";
    const main = document.createElement("div"); main.className = "command-main";
    const icon = document.createElement("div"); icon.className = "command-symbol"; icon.textContent = symbol[command.category] || "?";
    const content = document.createElement("div");
    const title = document.createElement("h3"); title.textContent = `/${command.name || "command"}`;
    const description = document.createElement("p"); description.textContent = command.description || "";
    const category = document.createElement("span"); category.className = "command-category"; category.textContent = command.category || "other";
    content.append(title, description, category); main.append(icon, content); card.appendChild(main); list.appendChild(card);
  }
}

function filterCommands() {
  const search = document.getElementById("commandSearch").value.toLowerCase();
  const category = document.getElementById("commandCategory").value;
  document.querySelectorAll(".command-card").forEach(card => {
    const matches = card.dataset.name.toLowerCase().includes(search) && (category === "all" || card.dataset.category === category);
    card.style.display = matches ? "flex" : "none";
  });
}

const categorySelect = document.getElementById("commandCategory");
const categoryButton = document.getElementById("commandCategoryButton");
const categoryMenu = document.querySelector("#commandCategorySelect .custom-select-menu");
document.querySelectorAll("#commandCategorySelect [data-category]").forEach(option => {
  option.addEventListener("click", () => {
    categorySelect.value = option.dataset.category;
    categoryButton.firstChild.textContent = option.textContent + " ";
    document.querySelectorAll("#commandCategorySelect [data-category]").forEach(item => item.setAttribute("aria-selected", item === option ? "true" : "false"));
    categoryMenu.classList.remove("open");
    categoryButton.setAttribute("aria-expanded", "false");
    filterCommands();
  });
});
categoryButton.addEventListener("click", event => {
  event.stopPropagation();
  document.querySelectorAll(".custom-select-menu.open").forEach(other => {
    if (other !== categoryMenu) { other.classList.remove("open"); const trigger = other.parentElement.querySelector(".custom-select-trigger"); if (trigger) trigger.setAttribute("aria-expanded", "false"); }
  });
  const open = categoryMenu.classList.toggle("open");
  categoryButton.setAttribute("aria-expanded", open ? "true" : "false");
});
document.addEventListener("click", event => {
  if (!event.target.closest("#commandCategorySelect")) {
    categoryMenu.classList.remove("open");
    categoryButton.setAttribute("aria-expanded", "false");
  }
});
categoryButton.addEventListener("keydown", event => {
  if (event.key === "Escape") { categoryMenu.classList.remove("open"); categoryButton.setAttribute("aria-expanded", "false"); }
});
const enhancedSelects = new Map();
function enhanceSelect(select) {
  if (!select || enhancedSelects.has(select.id)) return;
  select.hidden = true;
  select.classList.add("visually-hidden");
  const wrapper = document.createElement("div"); wrapper.className = "custom-select"; wrapper.id = select.id + "Custom";
  const button = document.createElement("button"); button.type = "button"; button.className = "custom-select-trigger"; button.setAttribute("aria-haspopup", "listbox"); button.setAttribute("aria-expanded", "false");
  const menu = document.createElement("div"); menu.className = "custom-select-menu"; menu.setAttribute("role", "listbox"); menu.setAttribute("aria-label", select.id);
  wrapper.append(button, menu); select.parentNode.insertBefore(wrapper, select.nextSibling);
  const sync = () => {
    const selected = select.options[select.selectedIndex] || select.options[0];
    button.textContent = selected ? selected.textContent : "Select an option";
    const caret = document.createElement("span"); caret.textContent = "⌄"; button.appendChild(caret);
    menu.replaceChildren();
    [...select.options].forEach(option => {
      const item = document.createElement("button"); item.type = "button"; item.textContent = option.textContent; item.dataset.value = option.value; item.setAttribute("role", "option"); item.setAttribute("aria-selected", option.selected ? "true" : "false");
      item.addEventListener("click", () => { select.value = option.value; select.dispatchEvent(new Event("change", { bubbles: true })); menu.classList.remove("open"); button.setAttribute("aria-expanded", "false"); });
      menu.appendChild(item);
    });
  };
  button.addEventListener("click", event => {
    event.stopPropagation();
    document.querySelectorAll(".custom-select-menu.open").forEach(other => {
      if (other !== menu) { other.classList.remove("open"); const trigger = other.parentElement.querySelector(".custom-select-trigger"); if (trigger) trigger.setAttribute("aria-expanded", "false"); }
    });
    const open = menu.classList.toggle("open");
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
  button.addEventListener("keydown", event => { if (event.key === "Escape") { menu.classList.remove("open"); button.setAttribute("aria-expanded", "false"); } });
  select.addEventListener("change", sync);
  enhancedSelects.set(select.id, sync); sync();
}
document.querySelectorAll("select:not(#commandCategory)").forEach(enhanceSelect);
document.addEventListener("click", event => {
  document.querySelectorAll(".custom-select-menu.open").forEach(menu => {
    if (!event.target.closest(".custom-select")) { menu.classList.remove("open"); const trigger = menu.parentElement.querySelector(".custom-select-trigger"); if (trigger) trigger.setAttribute("aria-expanded", "false"); }
  });
});

const pages = ["home", "commands", "config", "stats", "stats-growth", "stats-music", "stats-reliability"];
let currentAnalyticsView = "overview";

function prepareAnalyticsGroups() {
  const grid = document.querySelector("#page-stats .stats-grid");
  if (!grid) return;
  let group = "overview";
  const groupBase = { overview: 0, growth: 100, music: 200, reliability: 300 };
  [...grid.children].forEach(child => {
    if (child.classList.contains("analytics-section-heading")) {
      group = child.id.replace("analytics-", "") || "overview";
    }
    child.dataset.analyticsGroup = group;
    child.style.order = groupBase[group] ?? 0;
  });
}

function applyAnalyticsView(view = "overview") {
  prepareAnalyticsGroups();
  currentAnalyticsView = view;
  const page = document.getElementById("page-stats");
  page?.classList.remove("analytics-view-overview", "analytics-view-growth", "analytics-view-music", "analytics-view-reliability");
  page?.classList.add("analytics-view-" + view);
  const editing = page?.classList.contains("widget-edit-mode");
  document.querySelectorAll("#page-stats .stats-grid > *").forEach(child => {
    const inView = view === "overview" ? child.dataset.analyticsGroup === "overview" : child.dataset.analyticsGroup === view;
    const customHidden = child.dataset.customHidden === "true";
    child.hidden = !inView || (customHidden && !editing);
  });
  if (typeof applyPanelLayout === "function") applyPanelLayout();
  const title = document.getElementById("statsPageTitle");
  const subtitle = document.getElementById("statsPageSubtitle");
  const copy = {
    overview: ["Statistics", "A high-level view of your server's growth, music, and system health."],
    growth: ["Growth & Retention", "Invite quality, member retention, growth comparisons, and risk signals."],
    music: ["Music Operations", "Ratings, voting activity, requesters, song logs, and provider reliability."],
    reliability: ["Reliability & Admin", "Sync failures, command activity, exports, audit history, and system health."],
  }[view] || ["Statistics", "Real numbers from BUII's own database and live connection — nothing here is simulated."];
  if (title) title.textContent = copy[0];
  if (subtitle) subtitle.textContent = copy[1];
}

function showPage(page) {
  if (!pages.includes(page)) page = "home";
  const basePage = page.startsWith("stats-") ? "stats" : page;
  const analyticsView = page === "stats" ? "overview" : page.startsWith("stats-") ? page.replace("stats-", "") : null;
  document.querySelectorAll(".page").forEach(s => s.classList.remove("active-page"));
  const target = document.getElementById("page-" + basePage);
  if (target) target.classList.add("active-page");
  if (analyticsView) applyAnalyticsView(analyticsView);
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.page === page || (analyticsView && b.dataset.page === "stats")));
  document.querySelectorAll(".mobile-nav-item").forEach(b => b.classList.toggle("active", b.dataset.page === basePage));
  history.replaceState(null, "", "#" + page);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.querySelectorAll("[data-page]").forEach(btn => btn.addEventListener("click", () => {
  showPage(btn.dataset.page);
  closeMenus();
}));
document.getElementById("viewActivityButton").addEventListener("click", () => { showPage("stats"); closeMenus(); });
document.getElementById("refreshRecentJoinsButton").addEventListener("click", () => loadRecentJoins(currentGuildId));
function downloadExport(kind) {
  if (!currentGuildId) return;
  window.location.href = `/api/guilds/${currentGuildId}/exports/${kind}`;
}
document.getElementById("exportJoinsButton")?.addEventListener("click", () => downloadExport("joins"));
document.getElementById("exportSongsButton")?.addEventListener("click", () => downloadExport("songs"));
document.getElementById("exportActivityButton")?.addEventListener("click", () => downloadExport("activity"));
document.getElementById("refreshDashboardButton").addEventListener("click", async () => {
  if (!currentGuildId) return;
  const button = document.getElementById("refreshDashboardButton");
  button.disabled = true;
  await Promise.allSettled([loadOverview(currentGuildId), loadGrowthChart(currentGuildId), loadAnalytics(currentGuildId), loadLeaderboard(currentGuildId), loadMusicLeaderboard(currentGuildId), loadMusicLog(currentGuildId), loadMusicInsights(currentGuildId), loadRecentJoins(currentGuildId), loadConfig(currentGuildId)]);
  button.disabled = false;
  toast("Dashboard refreshed just now");
});
document.getElementById("commandSearch").addEventListener("input", filterCommands);
document.getElementById("commandCategory").addEventListener("change", filterCommands);
document.getElementById("growthPeriod").addEventListener("change", () => loadGrowthChart(currentGuildId));

document.querySelectorAll("[data-save-field]").forEach(button => {
  button.addEventListener("click", () => {
    const field = button.dataset.saveField;
    const values = {
      prefix: () => document.getElementById("cfgPrefix").value,
      log_channel_id: () => valueOrNull("cfgLogChannel"),
      alert_role_id: () => valueOrNull("cfgAlertRole"),
      mod_role_id: () => valueOrNull("cfgModRole"),
      music_channel_id: () => valueOrNull("cfgMusicChannel"),
      music_role_id: () => valueOrNull("cfgMusicRole"),
      music_lock_seconds: () => Number(document.getElementById("cfgLockSeconds").value || 0),
    };
    saveConfigField(field, values[field]());
  });
});

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

const profileBtn = document.getElementById("profileBtn");
const profileMenu = document.getElementById("profileMenu");
const notificationBtn = document.getElementById("notificationBtn");
const notificationMenu = document.getElementById("notificationsMenu");
const themeMenu = document.getElementById("themeMenu");

profileBtn.addEventListener("click", e => {
  e.stopPropagation();
  const open = profileMenu.classList.toggle("open");
  profileBtn.setAttribute("aria-expanded", open);
  if (open) { notificationMenu.classList.remove("open"); notificationBtn.setAttribute("aria-expanded", "false"); }
});
notificationBtn.addEventListener("click", e => {
  e.stopPropagation();
  const open = notificationMenu.classList.toggle("open");
  notificationBtn.setAttribute("aria-expanded", open);
  if (open) {
    profileMenu.classList.remove("open");
    profileBtn.setAttribute("aria-expanded", "false");
    const newest = dashboardState.recentRows[0];
    if (newest) {
      localStorage.setItem("buii-notifications-seen", `${newest.join_date || ""}|${newest.user_name || ""}`);
      document.getElementById("notificationDot").classList.remove("show");
    }
  }
});
function closeMenus() {
  profileMenu.classList.remove("open");
  notificationMenu.classList.remove("open");
  themeMenu.classList.remove("open");
  profileBtn.setAttribute("aria-expanded", "false");
  notificationBtn.setAttribute("aria-expanded", "false");
}
document.addEventListener("click", e => {
  if (!e.target.closest(".menu-wrapper") && !e.target.closest("#themeMenu") && e.target.id !== "themeBtn") closeMenus();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenus(); });

const themes = [
  ["default", "AMOLED", "#ffffff"], ["midnight", "Midnight", "#8b7cff"], ["ocean", "Ocean", "#087f91"],
  ["lavender", "Lavender", "#8254d6"], ["sunset", "Sunset", "#e64c35"], ["graphite", "Graphite", "#e9eaec"],
  ["forest", "Forest Night", "#64d58d"], ["paper", "Paper + Orange", "#ff5b22"], ["rose", "Rose", "#e85d8b"],
  ["cyber", "Cyber", "#35e6ff"], ["cherry", "Cherry", "#ff5470"], ["sapphire", "Sapphire", "#4d9cff"],
  ["matcha", "Matcha", "#6c9d45"], ["mono", "Mono", "#111111"],
];
const root = document.documentElement;
const themeOptions = document.getElementById("themeOptions");
themes.forEach(([id, name, color]) => {
  const button = document.createElement("button");
  button.className = "theme-option";
  button.dataset.theme = id;
  const swatch = document.createElement("span"); swatch.className = "swatch"; swatch.style.setProperty("--swatch", color);
  const label = document.createElement("span"); label.textContent = name;
  button.append(swatch, label);
  button.addEventListener("click", () => { applyTheme(id); themeMenu.classList.remove("open"); toast(name + " theme applied"); });
  themeOptions.appendChild(button);
});
function applyTheme(theme) {
  if (!theme || theme === "default") root.removeAttribute("data-theme");
  else root.dataset.theme = theme;
  localStorage.setItem("buii-theme", theme || "default");
  document.querySelectorAll(".theme-option").forEach(o => o.classList.toggle("active", o.dataset.theme === (theme || "default")));
}
applyTheme(localStorage.getItem("buii-theme") || "default");
document.getElementById("themeBtn").addEventListener("click", e => {
  e.stopPropagation();
  themeMenu.classList.toggle("open");
  profileMenu.classList.remove("open");
  notificationMenu.classList.remove("open");
});


const panelCustomizer = document.getElementById("panelCustomizer");
const panelCustomizerList = document.getElementById("panelCustomizerList");
const customizePanelsButton = document.getElementById("customizePanelsButton");
const resetWidgetLayoutButton = document.getElementById("resetWidgetLayoutButton");
const PANEL_PREFS_KEY = "buii-analytics-panels";
let widgetEditMode = false;

function panelStorageKey() {
  return `${PANEL_PREFS_KEY}:${currentGuildId || "default"}`;
}

function analyticsPanels() {
  return [...document.querySelectorAll("#page-stats .stats-grid > .card:not(.analytics-number):not(.analytics-detail)")];
}

function panelTitle(card, index) {
  return card.querySelector("h2")?.textContent?.trim() || `Panel ${index + 1}`;
}

function panelKey(card, index) {
  if (!card.dataset.panelKey) {
    const slug = panelTitle(card, index).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    card.dataset.panelKey = `panel-${slug || index}`;
  }
  return card.dataset.panelKey;
}

function readPanelPrefs() {
  try {
    const value = JSON.parse(localStorage.getItem(panelStorageKey()) || "{}");
    return {
      hidden: value.hidden && typeof value.hidden === "object" ? value.hidden : {},
      pinned: value.pinned && typeof value.pinned === "object" ? value.pinned : {},
      order: value.order && typeof value.order === "object" ? value.order : {},
      sizes: value.sizes && typeof value.sizes === "object" ? value.sizes : {},
    };
  } catch (_) {
    return { hidden: {}, pinned: {}, order: {}, sizes: {} };
  }
}

function writePanelPrefs(prefs) {
  localStorage.setItem(panelStorageKey(), JSON.stringify(prefs));
}

function applyPanelLayout() {
  const prefs = readPanelPrefs();
  const cards = analyticsPanels();
  const groups = {};
  const groupBases = { overview: 0, growth: 100, music: 200, reliability: 300 };
  cards.forEach((card, index) => {
    const key = panelKey(card, index);
    const group = card.dataset.analyticsGroup || "overview";
    (groups[group] ||= []).push({ card, key, index });
    card.classList.toggle("panel-pinned", prefs.pinned[key] === true);
    card.classList.remove("widget-size-1", "widget-size-2", "widget-size-4");
    if ([1, 2, 4].includes(Number(prefs.sizes[key]))) card.classList.add(`widget-size-${prefs.sizes[key]}`);
  });
  Object.entries(groups).forEach(([group, entries]) => {
    const savedOrder = Array.isArray(prefs.order[group]) ? prefs.order[group] : [];
    const orderMap = new Map(savedOrder.map((key, index) => [key, index]));
    entries.sort((a, b) => (orderMap.get(a.key) ?? 9999) - (orderMap.get(b.key) ?? 9999));
    const groupBase = groupBases[group] ?? 0;
    entries.forEach((entry, index) => {
      const pinnedOffset = prefs.pinned[entry.key] === true ? 0 : 100;
      entry.card.style.order = groupBase + 1 + pinnedOffset + index;
    });
  });
}

function applyPanelPrefs() {
  const prefs = readPanelPrefs();
  analyticsPanels().forEach((card, index) => {
    const key = panelKey(card, index);
    card.dataset.customHidden = prefs.hidden[key] === false ? "true" : "false";
  });
  if (typeof applyAnalyticsView === "function") applyAnalyticsView(currentAnalyticsView);
  applyPanelLayout();
}

function savePanelOrder(group) {
  const prefs = readPanelPrefs();
  prefs.order[group] = analyticsPanels()
    .filter(card => (card.dataset.analyticsGroup || "overview") === group)
    .map((card, index) => panelKey(card, index));
  writePanelPrefs(prefs);
  applyPanelLayout();
}

function makeWidgetButton(text, title, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `widget-control-button ${className}`.trim();
  button.textContent = text;
  button.title = title;
  button.setAttribute("aria-label", title);
  return button;
}

function renderWidgetControls() {
  analyticsPanels().forEach((card, index) => {
    card.querySelector(".widget-controls")?.remove();
    card.draggable = widgetEditMode;
    if (!widgetEditMode) return;
    const key = panelKey(card, index);
    const prefs = readPanelPrefs();
    const controls = document.createElement("div");
    controls.className = "widget-controls";
    controls.addEventListener("mousedown", event => event.stopPropagation());

    const grip = document.createElement("span");
    grip.className = "widget-grip";
    grip.textContent = "⋮⋮";
    grip.title = "Drag this widget to reorder";
    controls.appendChild(grip);

    const sizes = document.createElement("span");
    sizes.className = "widget-size-controls";
    [1, 2, 4].forEach(size => {
      const button = makeWidgetButton(`${size}×1`, `Set ${size} column widget size`, Number(prefs.sizes[key]) === size ? "active" : "");
      button.addEventListener("click", event => {
        event.stopPropagation();
        const nextPrefs = readPanelPrefs();
        nextPrefs.sizes[key] = size;
        writePanelPrefs(nextPrefs);
        applyPanelLayout();
        renderWidgetControls();
      });
      sizes.appendChild(button);
    });
    controls.appendChild(sizes);

    const pin = makeWidgetButton(prefs.pinned[key] === true ? "★" : "☆", prefs.pinned[key] === true ? "Unpin widget" : "Pin widget", prefs.pinned[key] === true ? "active" : "");
    pin.addEventListener("click", event => {
      event.stopPropagation();
      const nextPrefs = readPanelPrefs();
      nextPrefs.pinned[key] = nextPrefs.pinned[key] !== true;
      writePanelPrefs(nextPrefs);
      applyPanelLayout();
      renderWidgetControls();
    });
    controls.appendChild(pin);

    const visibility = makeWidgetButton(card.dataset.customHidden === "true" ? "Show" : "Hide", card.dataset.customHidden === "true" ? "Show widget" : "Hide widget");
    visibility.addEventListener("click", event => {
      event.stopPropagation();
      const nextPrefs = readPanelPrefs();
      const show = card.dataset.customHidden === "true";
      nextPrefs.hidden[key] = show;
      writePanelPrefs(nextPrefs);
      card.dataset.customHidden = show ? "false" : "true";
      applyAnalyticsView(currentAnalyticsView);
      renderWidgetControls();
    });
    controls.appendChild(visibility);
    card.appendChild(controls);
    card.classList.toggle("widget-hidden-preview", card.dataset.customHidden === "true");

    card.ondragstart = event => {
      if (!widgetEditMode) return;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", key);
      card.classList.add("widget-dragging");
    };
    card.ondragend = () => card.classList.remove("widget-dragging");
    card.ondragover = event => {
      if (widgetEditMode) event.preventDefault();
    };
    card.ondrop = event => {
      if (!widgetEditMode) return;
      event.preventDefault();
      const sourceKey = event.dataTransfer.getData("text/plain");
      const source = analyticsPanels().find(item => item.dataset.panelKey === sourceKey);
      const group = card.dataset.analyticsGroup || "overview";
      if (!source || source === card || source.dataset.analyticsGroup !== group) return;
      card.before(source);
      savePanelOrder(group);
      renderWidgetControls();
    };
  });
}

function setWidgetEditMode(enabled) {
  widgetEditMode = enabled;
  const page = document.getElementById("page-stats");
  page?.classList.toggle("widget-edit-mode", enabled);
  if (panelCustomizer) panelCustomizer.hidden = true;
  if (resetWidgetLayoutButton) resetWidgetLayoutButton.hidden = !enabled;
  if (customizePanelsButton) {
    customizePanelsButton.textContent = enabled ? "Done editing" : "Edit panels";
    customizePanelsButton.setAttribute("aria-expanded", enabled ? "true" : "false");
  }
  applyAnalyticsView(currentAnalyticsView);
  renderWidgetControls();
}

customizePanelsButton?.addEventListener("click", () => setWidgetEditMode(!widgetEditMode));
resetWidgetLayoutButton?.addEventListener("click", () => {
  localStorage.removeItem(panelStorageKey());
  applyPanelPrefs();
  renderWidgetControls();
  toast("Widget layout reset for this server");
});
applyPanelPrefs();

boot();

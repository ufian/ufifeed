"use strict";
const $ = (id) => document.getElementById(id);
const tg = window.Telegram?.WebApp;
let state, page = "overview", dirty = false, formRevision, journalCursor = null;
let historyCursor = null, historyMessage = null, timer, journalRequest = 0;
let briefingSignature = "";
const node = (tag, text, className) => {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
};
function notice(text) { $("notice").textContent = text; $("notice").hidden = false; }
function date(value) {
  if (!value) return "Not synchronized yet";
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: state?.schedule.timezone || "America/Los_Angeles" }).format(new Date(value));
}
async function api(path, data = {}) {
  const response = await fetch(`./api/${path}`, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", Authorization: `tma ${tg.initData}` }, body: JSON.stringify(data) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Connection unavailable. Try again.");
  return result;
}
async function perform(action, button) {
  if (button) button.disabled = true;
  try { await action(); } catch (error) { notice(error.message); }
  finally { if (button) button.disabled = false; }
}
function safeDigest(text) {
  const parsed = new DOMParser().parseFromString(text, "text/html");
  const copy = (item, parent) => {
    if (item.nodeType === Node.TEXT_NODE) { parent.append(document.createTextNode(item.textContent)); return; }
    if (item.nodeType !== Node.ELEMENT_NODE) return;
    let el;
    if (item.tagName === "B") el = node("b");
    else if (item.tagName === "A" && /^https:\/\/t\.me\/[A-Za-z0-9_\/]+$/.test(item.getAttribute("href") || "")) {
      el = node("a"); el.href = item.getAttribute("href"); el.target = "_blank"; el.rel = "noopener noreferrer";
    } else if (item.tagName === "BLOCKQUOTE") {
      const details = node("details", undefined, "digest-details");
      details.append(node("summary", "Details"));
      const body = node("div");
      for (const child of item.childNodes) copy(child, body);
      details.append(body); parent.append(details); return;
    } else if (item.tagName === "BR") { parent.append(node("br")); return; }
    else el = document.createDocumentFragment();
    for (const child of item.childNodes) copy(child, el);
    parent.append(el);
  };
  const result = document.createDocumentFragment();
  for (const item of parsed.body.childNodes) copy(item, result);
  return result;
}
function render() {
  $("folder-label").textContent = state.folder || "YOUR READING LIST";
  $("metrics").replaceChildren(...[[state.sources.filter(s => s.enabled).length, "Reading sources"], [state.journal.messages, "Archived messages"], [state.journal.deleted, "Deleted, retained"]].map(([value, label]) => {
    const card = node("div", undefined, "metric"); card.append(node("strong", value.toLocaleString()), node("span", label)); return card;
  }));
  $("prepare").disabled = state.busy;
  const progress = state.ai_progress;
  $("job-status").textContent = state.busy ? (progress?.input_batches ? `Reading batch ${Math.min(progress.completed_batches + 1, progress.input_batches)} of ${progress.input_batches}…` : "Preparing your briefing…") : state.manual_job?.state === "queued" ? "Preview prepared and queued in Telegram." : "A preview arrives in your Telegram chat.";
  $("service-error").hidden = !state.error;
  $("service-error").textContent = state.error || "";
  $("schedule-label").textContent = state.schedule.enabled ? state.schedule.times.join(" · ") : "Schedule paused";
  const digestSignature = JSON.stringify([state.digests, state.schedule.timezone]);
  if (digestSignature !== briefingSignature) {
    const expanded = new Set([...$("briefings").querySelectorAll("details[open]")].map(el => el.dataset.key));
    $("briefings").replaceChildren();
  for (const run of state.digests) {
    const card = node("article", undefined, "digest-card");
    const meta = node("div", undefined, "digest-meta"); meta.append(node("span", date(run.created_at)), node("span", [...new Set(run.parts.map(p => p.status))].join(" · ")));
    card.append(meta);
    for (const [partIndex, part] of run.parts.entries()) {
      const body = node("div", undefined, "digest-text"); body.append(safeDigest(part.text || "This briefing has expired."));
      for (const [index, details] of [...body.querySelectorAll("details")].entries()) {
        details.dataset.key = `${run.id}:${partIndex}:${index}`;
        details.open = expanded.has(details.dataset.key);
      }
      card.append(body);
    }
    $("briefings").append(card);
  }
  if (!state.digests.length) $("briefings").append(node("div", "Your first briefing will appear here. Prepare a preview to get started.", "empty"));
    briefingSignature = digestSignature;
  }
  renderSources();
  if (!dirty) renderSettings();
  const selected = $("journal-source").value;
  $("journal-source").replaceChildren(new Option("All sources", ""), ...state.sources.map(s => new Option(`${s.title}${s.enabled ? "" : " (archived)"}`, s.id)));
  $("journal-source").value = selected;
}
function renderSources() {
  $("current-folder").textContent = state.folder || "Choose a reader folder";
  $("folder-ai").checked = state.ai_folder;
  $("source-list").replaceChildren();
  for (const source of state.sources.filter(s => s.enabled)) {
    const card = node("article", undefined, "source-card"); card.append(node("div", source.title, "source-name"), node("p", `${source.kind === "group" ? "Group discussion" : "World news channel"} · ${source.messages} messages`, "muted"));
    const label = node("label", undefined, "check"), check = node("input"); check.type = "checkbox"; check.checked = Boolean(source.ai_allowed);
    check.onchange = () => perform(async () => { const result = await api("action", { action: "ai_source", id: source.id, enabled: check.checked, revision: state.revision }); notice(result.message); await refresh(); }, check);
    label.append(check, node("span", "AI access")); card.append(label, node("p", source.error || `Last sync: ${date(source.last_sync)}`, source.error ? "warning" : "muted")); $("source-list").append(card);
  }
  if (!state.sources.some(s => s.enabled)) $("source-list").append(node("div", "No active sources. Choose a folder or wait for synchronization.", "empty"));
}
function renderSettings() {
  formRevision = state.revision;
  for (const id of ["language", "mode"]) $(id).value = state[id];
  $("schedule-enabled").checked = state.schedule.enabled;
  $("times").value = state.schedule.times.join(", "); $("timezone").value = state.schedule.timezone;
  $("topics").value = state.topics.join(", "); $("exclude").value = state.exclude.join(", ");
  $("retention-label").textContent = state.retention_days ? `Message history is retained for ${state.retention_days} days.` : "Message history is retained indefinitely. Media files are not downloaded.";
}
async function refresh() { state = await api("state"); render(); }
async function loadJournal(reset = true) {
  const sequence = ++journalRequest;
  const source = $("journal-source").value;
  const result = await api("journal", { source_id: source ? Number(source) : null, deleted_only: $("deleted-only").checked, before: reset ? 0 : journalCursor });
  if (sequence !== journalRequest) return;
  if (reset) $("messages").replaceChildren();
  for (const message of result.messages) {
    const card = node("article", undefined, `message-card${message.deleted ? " deleted" : ""}`);
    const meta = node("div", undefined, "message-meta"); meta.append(node("strong", message.title), node("span", date(message.date)));
    card.append(meta);
    if (message.deleted) card.append(node("span", "Deleted · retained in journal", "badge deleted"));
    card.append(node("p", message.text || (message.media ? `${message.media} · no text` : "Content was not observed before deletion."), "message-text"));
    const actions = node("div", undefined, "message-actions");
    actions.append(node("span", `${message.reactions ? `${message.reactions} reactions · ` : ""}#${message.id}${message.edited_at ? " · edited" : ""}`, "muted"));
    const button = node("button", "Versions", "secondary"); button.onclick = () => perform(() => openHistory(message), button); actions.append(button); card.append(actions); $("messages").append(card);
  }
  if (!$("messages").children.length) $("messages").append(node("div", "No matching messages in the journal yet.", "empty"));
  journalCursor = result.next; $("more-messages").hidden = !journalCursor;
}
async function openHistory(message, append = false) {
  historyMessage = message;
  const result = await api("history", { source_id: message.source_id, message_id: message.id, before: append ? historyCursor : 0 });
  if (!append) $("history-events").replaceChildren();
  for (const event of result.events) {
    const card = node("article", undefined, "event-card"); card.append(node("span", event.kind, "badge"), node("p", `Observed ${date(event.observed_at)}${event.snapshot.edited_at ? ` · Edited ${date(event.snapshot.edited_at)}` : ""}`, "muted"), node("p", event.snapshot.text || "No text was captured.", "event-text"));
    if (event.snapshot.reaction_count != null) card.append(node("p", `${event.snapshot.reaction_count} organic reactions`, "muted"));
    $("history-events").append(card);
  }
  if (!$("history-events").children.length) $("history-events").append(node("p", "No observed versions."));
  historyCursor = result.next; $("more-history").hidden = !historyCursor;
  if (!$("history-dialog").open) $("history-dialog").showModal();
}
async function navigate(next) {
  page = next;
  for (const section of document.querySelectorAll(".page")) section.hidden = section.id !== next;
  for (const button of document.querySelectorAll(".tabs button")) button.toggleAttribute("aria-current", button.dataset.page === next);
  document.querySelector(`[data-page="${next}"]`).setAttribute("aria-current", "page");
  if (next === "journal") await loadJournal();
}
if (tg?.initData) {
  tg.ready(); tg.expand();
  $("welcome").hidden = true; $("application").hidden = false;
  for (const button of document.querySelectorAll(".tabs button")) button.onclick = () => perform(() => navigate(button.dataset.page));
  $("refresh").onclick = () => perform(refresh, $("refresh"));
  $("prepare").onclick = () => perform(async () => { const result = await api("digest"); notice(result.message); await refresh(); }, $("prepare"));
  $("preferences").oninput = () => { dirty = true; };
  $("preferences").onsubmit = (event) => { event.preventDefault(); perform(async () => {
    const split = (id) => $(id).value.split(",").map(v => v.trim()).filter(Boolean);
    const result = await api("settings", { revision: formRevision, language: $("language").value, mode: $("mode").value, topics: split("topics"), exclude: split("exclude"), schedule: { enabled: $("schedule-enabled").checked, times: split("times"), timezone: $("timezone").value.trim() } });
    dirty = false; state = result; render(); notice("Preferences saved.");
  }, event.submitter); };
  $("reload-settings").onclick = () => perform(async () => { dirty = false; await refresh(); notice("Saved preferences loaded."); }, $("reload-settings"));
  $("load-folders").onclick = () => perform(async () => { const result = await api("folders"); $("folder-select").replaceChildren(...result.folders.map(f => new Option(f.title, f.id))); $("folder-select").value = state.folder_id; $("folder-picker").hidden = false; }, $("load-folders"));
  $("choose-folder").onclick = () => perform(async () => { const result = await api("action", { action: "folder", id: Number($("folder-select").value), revision: state.revision }); notice(result.message); $("folder-picker").hidden = true; await refresh(); }, $("choose-folder"));
  $("folder-ai").onchange = () => perform(async () => { const result = await api("action", { action: "ai_folder", enabled: $("folder-ai").checked, revision: state.revision }); notice(result.message); await refresh(); }, $("folder-ai"));
  $("journal-source").onchange = $("deleted-only").onchange = () => perform(() => loadJournal());
  $("more-messages").onclick = () => perform(() => loadJournal(false), $("more-messages"));
  $("close-history").onclick = () => $("history-dialog").close();
  $("more-history").onclick = () => perform(() => openHistory(historyMessage, true), $("more-history"));
  perform(refresh).then(() => { timer = setInterval(() => { if (!document.hidden) perform(refresh); }, 15000); });
  window.addEventListener("pagehide", () => clearInterval(timer));
}

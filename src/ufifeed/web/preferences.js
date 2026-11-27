"use strict";

const webApp = window.Telegram?.WebApp;
const status = document.querySelector("#status");
const form = document.querySelector("#preferences");
const sources = document.querySelector("#sources");
const followAll = document.querySelector("#follow-all");
const save = document.querySelector("#save");
let revision;

async function request(action, data = {}) {
  const response = await fetch(`/api/preferences/${action}`, {
    method: "POST",
    headers: {
      Authorization: `tma ${webApp.initData}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

function render(data) {
  revision = data.revision;
  followAll.checked = data.follow_all;
  document.querySelector("#follow-label").hidden = !data.owner;
  sources.replaceChildren();
  for (const source of data.sources) {
    const label = document.createElement("label");
    label.className = "check";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = source.id;
    input.checked = source.selected;
    input.disabled = followAll.checked;
    label.append(input, document.createTextNode(source.title));
    sources.append(label);
  }
  if (!data.sources.length) sources.textContent = "No sources are available.";
  form.hidden = false;
}

followAll.addEventListener("change", () => {
  for (const input of sources.querySelectorAll("input")) {
    input.disabled = followAll.checked;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  save.disabled = true;
  try {
    const source_ids = followAll.checked
      ? null
      : [...sources.querySelectorAll("input:checked")].map(input => Number(input.value));
    render(await request("save", {revision, source_ids}));
    status.textContent = "Digest sources saved.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    save.disabled = false;
  }
});

if (webApp?.initData) {
  webApp.ready();
  status.textContent = "Loading sources…";
  request("state").then(data => {
    render(data);
    status.textContent = "";
  }).catch(error => {
    status.textContent = error.message;
  });
}

const SECTIONS = [
  { key: "overdue", title: "Overdue", hint: "these already slipped - fix them first" },
  { key: "due_soon", title: "Due within 24 hours", hint: "" },
  { key: "week", title: "Due this week", hint: "" },
  { key: "later", title: "Later", hint: "no fixed date or further out" },
  { key: "done", title: "Completed", hint: "" },
];

const board = document.getElementById("board");

async function load() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    const data = await res.json();
    render(data);
  } catch (err) {
    board.innerHTML = `<div class="empty">Lost contact with the watcher: ${err}</div>`;
  }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function card(item, group) {
  const chips = [];
  if (item.category) chips.push(`<span class="chip category">${esc(item.category)}</span>`);
  if (item.urgency) chips.push(`<span class="chip ${esc(item.urgency)}">${esc(item.urgency)}</span>`);
  if (item.deadline_inferred) chips.push(`<span class="chip inferred">inferred date</span>`);

  const meta = [];
  if (item.deadline_human) meta.push(`<span>&#128197; ${esc(item.deadline_human)} (${esc(item.relative)})</span>`);
  if (item.source_label) meta.push(`<span>&#127968; ${esc(item.source_label)}</span>`);
  if (item.source_url) {
    meta.push(`<a href="${esc(item.source_url)}" target="_blank" rel="noreferrer">open &#8599;</a>`);
  }

  let actions;
  if (group === "done") {
    actions = `<button data-action="open" data-id="${item.id}">Reopen</button>`;
  } else {
    actions = `
      <button class="primary" data-action="done" data-id="${item.id}">Done</button>
      <button data-action="snooze" data-id="${item.id}" data-hours="24">+1d</button>
      <button data-action="snooze" data-id="${item.id}" data-hours="72">+3d</button>`;
  }

  return `
    <div class="card ${esc(group)}">
      <div class="card-body">
        <div class="title">${esc(item.title)}</div>
        ${item.action ? `<div class="action">${esc(item.action)}</div>` : ""}
        <div class="meta">${chips.join(" ")}</div>
        <div class="meta">${meta.join("")}</div>
      </div>
      <div class="card-actions">${actions}</div>
    </div>`;
}

function render(data) {
  const groups = data.groups || {};
  const stats = data.stats || {};

  document.getElementById("stat-open").textContent = stats.open ?? 0;
  document.getElementById("stat-emails").textContent = stats.emails_seen ?? 0;
  document.getElementById("stat-done").textContent = stats.done ?? 0;
  document.getElementById("stat-overdue").textContent = (groups.overdue || []).length;
  document.getElementById("stat-overdue").parentElement.classList.toggle(
    "danger",
    (groups.overdue || []).length > 0
  );

  const next = (groups.due_soon || [])[0] || (groups.week || [])[0];
  document.getElementById("subtitle").textContent = next
    ? `next up: ${next.title} — ${next.relative}`
    : "nothing pending right now";
  document.getElementById("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;

  board.innerHTML = SECTIONS.map((section) => {
    const items = groups[section.key] || [];
    if (!items.length) return "";
    return `
      <section>
        <div class="section-head">
          <h2>${esc(section.title)}</h2>
          <span class="count">${items.length}</span>
          ${section.hint ? `<span class="count">${esc(section.hint)}</span>` : ""}
        </div>
        ${items.map((item) => card(item, section.key)).join("")}
      </section>`;
  }).join("") || `<div class="empty">Nothing tracked yet.<br>Run <code>python -m src.main run</code> to scan your inbox.</div>`;
}

board.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  try {
    await fetch(`/api/items/${button.dataset.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: button.dataset.action,
        hours: Number(button.dataset.hours || 24),
      }),
    });
    await load();
  } finally {
    button.disabled = false;
  }
});

load();
setInterval(load, 30000);

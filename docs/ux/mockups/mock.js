// OmniaCLI mockups: menus, dialogs and clickable rows; nothing is sent anywhere.
document.addEventListener("click", (ev) => {
  const toggle = ev.target.closest("[data-menu]");
  for (const m of document.querySelectorAll(".menu.open")) if (!m.contains(ev.target)) m.classList.remove("open");
  if (toggle) toggle.closest(".menu").classList.toggle("open");
  const open = ev.target.closest("[data-open]");
  if (open) document.getElementById(open.dataset.open).showModal();
  const close = ev.target.closest("[data-close]");
  if (close) close.closest("dialog").close();
  const row = ev.target.closest("tr[data-href]");
  if (row && !ev.target.closest("a, button")) location.href = row.dataset.href;
  const fake = ev.target.closest("[data-say]");
  if (fake) { alert(fake.dataset.say); }
});

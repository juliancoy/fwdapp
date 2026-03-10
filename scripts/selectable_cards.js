(() => {
  const cards = document.querySelectorAll(".projects-grid .card");

  const openPrimaryLink = (link) => {
    if (link.target === "_blank") {
      window.open(link.href, "_blank", "noopener");
      return;
    }
    window.location.href = link.href;
  };

  cards.forEach((card) => {
    const primaryLink =
      card.querySelector("a.project-title-link[href]") ||
      card.querySelector("a.btn.fill[href]") ||
      card.querySelector("a.btn[href]");
    if (!primaryLink) return;

    card.classList.add("selectable-card");
    card.setAttribute("tabindex", "0");
    card.setAttribute("role", "link");
    card.setAttribute("aria-label", primaryLink.textContent.trim());

    card.addEventListener("mouseenter", () => card.classList.add("is-highlighted"));
    card.addEventListener("mouseleave", () => card.classList.remove("is-highlighted"));
    card.addEventListener("focusin", () => card.classList.add("is-highlighted"));
    card.addEventListener("focusout", () => card.classList.remove("is-highlighted"));

    card.addEventListener("click", (event) => {
      if (event.target.closest("a, button, input, textarea, select, label")) return;
      openPrimaryLink(primaryLink);
    });

    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      if (event.target.closest("a, button, input, textarea, select, label")) return;
      event.preventDefault();
      openPrimaryLink(primaryLink);
    });
  });
})();

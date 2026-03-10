(async () => {
  const MANIFEST_PATH = "images/previews/link-preview-manifest.json";
  let manifest = {};

  try {
    const response = await fetch(MANIFEST_PATH, { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    manifest = data.by_href || {};
  } catch {
    return;
  }

  const cards = document.querySelectorAll(".projects-grid .card");
  cards.forEach((card) => {
    const links = [...card.querySelectorAll("a.project-title-link[href], a.btn[href]")];
    if (!links.length) return;

    const linksWithPreviews = links.filter((link) => {
      const href = (link.getAttribute("href") || "").trim();
      return Boolean(manifest[href]);
    });
    if (!linksWithPreviews.length) return;

    const existingImage = card.querySelector("img.project-preview");
    if (linksWithPreviews.length === 1 && existingImage) {
      return;
    }

    let image = existingImage;
    if (!image) {
      image = document.createElement("img");
      image.className = "project-preview";
      image.alt = "Link preview";
      image.loading = "lazy";
      const heading = card.querySelector("h2, h3");
      if (heading && heading.parentNode) {
        heading.parentNode.insertBefore(image, heading.nextSibling);
      } else {
        card.prepend(image);
      }
    }

    const defaultLink = linksWithPreviews[0];
    let hideTimer;

    const clearHideTimer = () => {
      if (hideTimer) {
        window.clearTimeout(hideTimer);
        hideTimer = undefined;
      }
    };

    const showPreviewForLink = (link) => {
      const href = (link.getAttribute("href") || "").trim();
      const previewSrc = manifest[href];
      if (!previewSrc) return;

      clearHideTimer();
      image.src = previewSrc;
    };

    const queueHide = () => {
      clearHideTimer();
      hideTimer = window.setTimeout(() => {
        showPreviewForLink(defaultLink);
      }, 120);
    };

    linksWithPreviews.forEach((link) => {
      link.addEventListener("mouseenter", () => showPreviewForLink(link));
      link.addEventListener("focus", () => showPreviewForLink(link));
      link.addEventListener("mouseleave", queueHide);
      link.addEventListener("blur", queueHide);
    });

    card.addEventListener("mouseleave", queueHide);
    image.addEventListener("mouseenter", clearHideTimer);
    image.addEventListener("mouseleave", queueHide);

    showPreviewForLink(defaultLink);
  });
})();

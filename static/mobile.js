(() => {
    "use strict";

    const body = document.body;
    const sidebar = document.getElementById("sidebar");
    const openButton = document.getElementById("mobile-menu-button");
    const closeButton = document.getElementById("mobile-menu-close");
    const overlay = document.getElementById("mobile-overlay");

    const isMobile = () => window.matchMedia("(max-width: 900px)").matches;

    function openMenu() {
        if (!sidebar || !isMobile()) return;

        body.classList.add("mobile-menu-open");

        if (openButton) {
            openButton.setAttribute("aria-expanded", "true");
        }

        if (overlay) {
            overlay.setAttribute("aria-hidden", "false");
        }
    }

    function closeMenu() {
        body.classList.remove("mobile-menu-open");

        if (openButton) {
            openButton.setAttribute("aria-expanded", "false");
        }

        if (overlay) {
            overlay.setAttribute("aria-hidden", "true");
        }
    }

    if (openButton) {
        openButton.addEventListener("click", openMenu);
    }

    if (closeButton) {
        closeButton.addEventListener("click", closeMenu);
    }

    if (overlay) {
        overlay.addEventListener("click", closeMenu);
    }

    if (sidebar) {
        sidebar.querySelectorAll("nav a").forEach((link) => {
            link.addEventListener("click", () => {
                if (isMobile()) closeMenu();
            });
        });
    }

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMenu();
    });

    window.addEventListener("resize", () => {
        if (!isMobile()) closeMenu();
    });

    // Converte semanticamente as tabelas em cartões no celular.
    // O CSS usa data-label para exibir o nome de cada coluna.
    document.querySelectorAll(".table-wrap table").forEach((table) => {
        const headers = Array.from(
            table.querySelectorAll("thead th")
        ).map((header) => header.textContent.trim());

        table.querySelectorAll("tbody tr").forEach((row) => {
            const cells = Array.from(row.children);

            cells.forEach((cell, index) => {
                if (cell.hasAttribute("colspan")) {
                    cell.classList.add("mobile-empty-cell");
                    return;
                }

                if (!cell.dataset.label) {
                    cell.dataset.label = headers[index] || "";
                }
            });
        });
    });
})();

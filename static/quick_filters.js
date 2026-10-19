(() => {
    "use strict";

    const normalize = (value) =>
        String(value || "")
            .normalize("NFD")
            .replace(/[\u0300-\u036f]/g, "")
            .trim()
            .toLowerCase();

    function buildColumnMap(table) {
        const map = new Map();
        table.querySelectorAll("thead th").forEach((header, index) => {
            map.set(normalize(header.textContent), index);
        });
        return map;
    }

    function initFilterGroup(group) {
        const targetId = group.dataset.filterTarget;
        const table = document.getElementById(targetId);
        if (!table) return;

        const columnMap = buildColumnMap(table);
        const search = group.querySelector("[data-filter-search]");
        const selects = Array.from(group.querySelectorAll("[data-filter-column]"));
        const clearButton = group.querySelector("[data-filter-clear]");
        const count = document.querySelector(`[data-filter-count="${targetId}"]`);

        const rows = Array.from(table.querySelectorAll("tbody tr"))
            .filter((row) => !row.querySelector("[colspan]"));

        const empty = document.createElement("div");
        empty.className = "quick-filter-empty";
        empty.textContent = "Nenhum lançamento corresponde aos filtros selecionados.";
        empty.hidden = true;

        const tableWrap = table.closest(".table-wrap");
        if (tableWrap) {
            tableWrap.insertAdjacentElement("afterend", empty);
        }

        function getCellText(row, label) {
            const index = columnMap.get(normalize(label));
            if (index === undefined) return "";
            const cell = row.children[index];
            return cell ? cell.textContent.trim() : "";
        }

        function update() {
            const query = normalize(search ? search.value : "");
            let visible = 0;

            rows.forEach((row) => {
                const matchesSearch =
                    !query || normalize(row.textContent).includes(query);

                const matchesSelects = selects.every((select) => {
                    if (!select.value) return true;

                    const actual = normalize(
                        getCellText(row, select.dataset.filterColumn)
                    );
                    const expected = normalize(select.value);

                    return actual === expected;
                });

                const show = matchesSearch && matchesSelects;
                row.hidden = !show;
                if (show) visible += 1;
            });

            if (count) {
                const total = Number(count.dataset.total || rows.length);
                count.textContent =
                    visible === total
                        ? `${total} ${total === 1 ? "item" : "itens"}`
                        : `${visible} de ${total}`;
            }

            empty.hidden = rows.length === 0 || visible !== 0;

            group.classList.toggle(
                "has-active-filter",
                Boolean(query) || selects.some((select) => Boolean(select.value))
            );
        }

        if (search) search.addEventListener("input", update);
        selects.forEach((select) => select.addEventListener("change", update));

        if (clearButton) {
            clearButton.addEventListener("click", () => {
                if (search) search.value = "";
                selects.forEach((select) => {
                    select.value = "";
                });
                update();
                if (search) search.focus();
            });
        }

        update();
    }

    document.querySelectorAll("[data-filter-target]").forEach(initFilterGroup);
})();

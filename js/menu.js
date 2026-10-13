document.addEventListener("DOMContentLoaded", () => {

    /*
     * Il menu viene letto da menu.json.
     *
     * - [data-menu="full"]     -> menu completo (menu.html)
     * - [data-menu="featured"] -> solo le voci con "featured": true (homepage)
     * - [data-menu-note]       -> nota su variazioni e allergeni
     */

    const fullMenu = document.querySelector('[data-menu="full"]');
    const featuredMenu = document.querySelector('[data-menu="featured"]');
    const menuNotes = document.querySelectorAll("[data-menu-note]");

    if (!fullMenu && !featuredMenu) {
        return;
    }


    fetch("menu.json")
        .then(response => {
            if (!response.ok) {
                throw new Error("Impossibile caricare menu.json");
            }

            return response.json();
        })
        .then(menu => {

            if (fullMenu) {
                renderFullMenu(menu);
            }

            if (featuredMenu) {
                renderFeatured(menu);
            }

            menuNotes.forEach(note => {
                note.textContent = menu.note || "";
            });
        })
        .catch(error => {

            console.error("Errore menu:", error);

            if (fullMenu) {
                fullMenu.innerHTML = `
                    <p class="menu-error">
                        Il menu non è disponibile al momento.
                        Scrivici su WhatsApp per informazioni.
                    </p>
                `;
            }

            if (featuredMenu) {
                featuredMenu.style.display = "none";
            }
        });


    /*
    ========================================
    MENU COMPLETO
    ========================================
    */

    function renderFullMenu(menu) {

        const categories = menu.sections
            .flatMap(section => section.categories);


        const nav = `
            <nav class="menu-nav" aria-label="Categorie del menu">
                ${categories.map(category => `
                    <a href="#${escapeHtml(category.id)}">
                        ${escapeHtml(category.title)}
                    </a>
                `).join("")}
            </nav>
        `;


        const columns = menu.sections
            .map(section => `
                <div class="menu-column">

                    <span class="menu-column-title">
                        ${escapeHtml(section.title.toUpperCase())}
                    </span>

                    ${section.categories.map(renderCategory).join("")}

                </div>
            `)
            .join("");


        fullMenu.innerHTML = `
            ${nav}

            <div class="menu-grid">
                ${columns}
            </div>
        `;
    }


    function renderCategory(category) {

        const columns = category.columns || null;

        let currentGroup = null;

        const items = visibleItems(category.items)
            .map(item => {

                let groupTitle = "";

                if (item.group && item.group !== currentGroup) {

                    currentGroup = item.group;

                    groupTitle = `
                        <h4 class="menu-group">
                            ${escapeHtml(item.group)}
                        </h4>
                    `;
                }

                return groupTitle + renderItem(item, columns);
            })
            .join("");


        const columnsHead = columns
            ? `
                <div class="menu-columns-head">
                    ${columns.map(label => `
                        <span>${escapeHtml(label)}</span>
                    `).join("")}
                </div>
            `
            : "";


        return `
            <div class="menu-category" id="${escapeHtml(category.id)}">

                <div class="menu-category-head">

                    <h3>
                        ${escapeHtml(category.title)}
                    </h3>

                    ${columnsHead}

                </div>

                ${items}

            </div>
        `;
    }


    function renderItem(item, columns) {

        const description = item.description
            ? `<small>${escapeHtml(item.description)}</small>`
            : "";


        /*
         * Una voce con un prezzo solo (es. birre in bottiglia)
         * usa la riga normale anche in una categoria a colonne.
         */
        if (columns && item.prices) {

            const prices = columns
                .map((label, index) => `
                    <span aria-label="${escapeHtml(label)}">
                        ${formatPrice((item.prices || [])[index])}
                    </span>
                `)
                .join("");

            return `
                <div class="beer-item">

                    <div class="menu-item-line">
                        <strong>${escapeHtml(item.name)}</strong>
                        <span class="menu-leader" aria-hidden="true"></span>
                        <div class="beer-prices">
                            ${prices}
                        </div>
                    </div>

                    ${description}

                </div>
            `;
        }


        /*
         * Nome, linea puntinata e prezzo sulla stessa riga,
         * descrizione sotto: l'occhio segue la riga fino al prezzo.
         */
        return `
            <div class="menu-item">

                <div class="menu-item-line">
                    <strong>${escapeHtml(item.name)}</strong>
                    <span class="menu-leader" aria-hidden="true"></span>
                    <span class="menu-price">${formatPrice(item.price)}</span>
                </div>

                ${description}

            </div>
        `;
    }


    /*
    ========================================
    ANTEPRIMA HOMEPAGE
    ========================================
    */

    function renderFeatured(menu) {

        const columns = menu.sections
            .map(section => {

                const items = section.categories
                    .flatMap(category => visibleItems(category.items))
                    .filter(item => item.featured)
                    .sort((a, b) => sortPrice(b) - sortPrice(a));

                if (items.length === 0) {
                    return "";
                }

                return `
                    <div class="menu-preview-column">

                        <span class="menu-column-title">
                            ${escapeHtml(section.title)}
                        </span>

                        ${items.map(renderFeaturedItem).join("")}

                    </div>
                `;
            })
            .join("");

        featuredMenu.innerHTML = columns;
    }


    function renderFeaturedItem(item) {

        /*
         * Nell'anteprima niente descrizioni: solo nome e prezzo,
         * così le righe delle due colonne restano allineate.
         * Per le voci con più prezzi (es. birre piccola/media)
         * si mostra il prezzo più basso.
         */
        const price = item.prices
            ? `da ${formatPrice(Math.min(...item.prices.filter(isPrice)))}`
            : formatPrice(item.price);

        return `
            <div class="menu-preview-item">

                <strong>${escapeHtml(item.name)}</strong>

                <span class="menu-leader" aria-hidden="true"></span>

                <span class="menu-price">${price}</span>

            </div>
        `;
    }


    /*
    ========================================
    UTILITÀ
    ========================================
    */

    /*
     * Una voce con "available": false resta nel file
     * ma non viene mostrata (es. piatto finito o sospeso).
     *
     * Le voci sono ordinate dal prezzo più alto (per le birre
     * conta il più basso, cioè la piccola, come nel "da" della
     * homepage, e a parità la media); i gruppi (es. vini
     * Rossi / Bianchi) restano nell'ordine del file e a parità
     * di prezzo resta l'ordine del file.
     */
    function visibleItems(items) {

        const visible = (items || []).filter(item => item.available !== false);

        const groups = [...new Set(visible.map(item => item.group || ""))];

        return visible.sort((a, b) =>
            groups.indexOf(a.group || "") - groups.indexOf(b.group || "") ||
            sortPrice(b) - sortPrice(a) ||
            highestPrice(b) - highestPrice(a)
        );
    }


    function highestPrice(item) {

        const prices = (item.prices || [item.price]).filter(isPrice);

        return prices.length > 0 ? Math.max(...prices) : 0;
    }


    function sortPrice(item) {

        const prices = (item.prices || [item.price]).filter(isPrice);

        return prices.length > 0 ? Math.min(...prices) : 0;
    }


    function isPrice(value) {
        return typeof value === "number" && !isNaN(value);
    }


    function formatPrice(value) {

        if (!isPrice(value)) {
            return "—";
        }

        return `€ ${value.toFixed(2).replace(".", ",")}`;
    }


    function escapeHtml(value) {

        return String(value)
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

});

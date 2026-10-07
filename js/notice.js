document.addEventListener("DOMContentLoaded", () => {

    /*
     * Avviso in fondo allo schermo (es. "Chiuso per ferie...").
     * Il testo sta in notice.json: vuoto = nessun avviso.
     * "expires" (facoltativo, data e ora ISO): dopo quel momento
     * l'avviso sparisce da solo (es. "Stasera chiudiamo alle 00:30").
     * Chi lo chiude non lo rivede finché il testo non cambia.
     */

    const STORAGE_KEY = "memphis-notice-closed";

    fetch("notice.json", { cache: "no-store" })
        .then(response => response.ok ? response.json() : null)
        .then(notice => {

            const text = notice && String(notice.text || "").trim();

            const expired = notice && notice.expires && new Date(notice.expires) < new Date();

            if (!text || expired || readClosed() === text) {
                return;
            }

            const bar = document.createElement("div");

            bar.className = "site-notice";
            bar.setAttribute("role", "status");

            const message = document.createElement("p");
            message.textContent = text;

            const close = document.createElement("button");
            close.type = "button";
            close.setAttribute("aria-label", "Chiudi avviso");
            close.textContent = "×";

            close.addEventListener("click", () => {
                bar.remove();
                saveClosed(text);
            });

            bar.append(message, close);
            document.body.append(bar);
        })
        .catch(error => console.error("Errore avviso:", error));


    function readClosed() {
        try {
            return sessionStorage.getItem(STORAGE_KEY);
        } catch (error) {
            return null;
        }
    }


    function saveClosed(text) {
        try {
            sessionStorage.setItem(STORAGE_KEY, text);
        } catch (error) {
            // Storage non disponibile: l'avviso tornerà al prossimo caricamento
        }
    }

});

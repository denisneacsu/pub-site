document.addEventListener("DOMContentLoaded", () => {

    /*
     * Navigazione mobile: il pulsante a tre linee
     * apre e chiude il pannello dei link.
     */

    const header = document.querySelector(".site-header");
    const toggle = document.querySelector(".nav-toggle");
    const nav = document.getElementById("site-nav");

    if (!header || !toggle || !nav) {
        return;
    }


    function setOpen(open) {

        header.classList.toggle("nav-open", open);

        toggle.setAttribute("aria-expanded", String(open));

        toggle.setAttribute(
            "aria-label",
            open
                ? "Chiudi il menu di navigazione"
                : "Apri il menu di navigazione"
        );
    }


    toggle.addEventListener("click", () => {
        setOpen(!header.classList.contains("nav-open"));
    });


    // Chiude il pannello quando si sceglie una sezione
    nav.querySelectorAll("a").forEach(link => {
        link.addEventListener("click", () => setOpen(false));
    });


    document.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            setOpen(false);
        }
    });

});

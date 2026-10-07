document.addEventListener("DOMContentLoaded", () => {

    const nextEventSection = document.querySelector(".next-event");
    const eventFeature = document.querySelector(".event-feature");
    const pastEventsGrid = document.querySelector(".past-events-grid");

    let events = [];
    let countdownInterval = null;
    let currentNextEvent = null;


    /*
    ========================================
    CARICAMENTO EVENTI
    ========================================
    */

    fetch("events.json")
        .then(response => {
            if (!response.ok) {
                throw new Error("Impossibile caricare events.json");
            }

            return response.json();
        })
        .then(data => {

            events = data
                .filter(event =>
                    event.date &&
                    event.title &&
                    event.image
                )
                .map(event => ({
                    ...event,
                    dateTime: createEventDate(
                        event.date,
                        event.time
                    )
                }))
                .filter(event =>
                    event.dateTime instanceof Date &&
                    !isNaN(event.dateTime)
                );

            renderPage();

            /*
             * Il countdown viene aggiornato ogni secondo,
             * ma NON ricostruiamo lo storico.
             */
            countdownInterval = setInterval(updateCountdown, 1000);
        })
        .catch(error => {

            console.error("Errore eventi:", error);

            if (nextEventSection) {
                nextEventSection.style.display = "none";
            }

            if (pastEventsGrid) {
                pastEventsGrid.innerHTML = "";
            }
        });


    /*
    ========================================
    DATA EVENTO
    ========================================
    */

    function createEventDate(date, time) {

        const eventTime =
            time && /^\d{2}:\d{2}$/.test(time)
                ? time
                : "23:59";

        return new Date(`${date}T${eventTime}:00`);
    }


    /*
    ========================================
    RENDER INIZIALE
    ========================================
    */

    function renderPage() {

        const now = new Date();

        const upcomingEvents = events
            .filter(event => event.dateTime > now)
            .sort((a, b) =>
                a.dateTime - b.dateTime
            );

        const pastEvents = events
            .filter(event => event.dateTime <= now)
            .sort((a, b) =>
                b.dateTime - a.dateTime
            );


        currentNextEvent = upcomingEvents[0] || null;


        /*
        ========================================
        PROSSIMO EVENTO
        ========================================
        */

        if (currentNextEvent) {

            nextEventSection.style.display = "";

            renderNextEvent(currentNextEvent);

        } else {

            nextEventSection.style.display = "none";
        }


        /*
        ========================================
        STORICO
        ========================================
        */

        renderPastEvents(pastEvents);
    }


    /*
    ========================================
    PROSSIMO EVENTO
    ========================================
    */

    function renderNextEvent(event) {

        const formattedDate =
            formatDate(event.dateTime);

        const whatsappText = encodeURIComponent(
            `Ciao! Vorrei informazioni sulla serata "${event.title}" del ${formattedDate}.`
        );


        eventFeature.innerHTML = `

            <div class="event-poster">

                <img
                    src="${event.image}"
                    alt="Locandina ${escapeHtml(event.title)}"
                >

            </div>


            <div class="event-info">

                <div class="event-head">

                    <span class="event-date">
                        ${formattedDate}
                        ${event.time ? ` · ${event.time}` : ""}
                    </span>


                    <h3>
                        ${escapeHtml(event.title)}
                    </h3>


                    <p>
                        Il prossimo appuntamento
                        del MEMPHIS①.
                    </p>

                </div>


                <div
                    class="event-countdown"
                    role="timer"
                    aria-label="Conto alla rovescia per ${escapeHtml(event.title)}"
                >

                    <div>
                        <strong data-countdown="days">
                            00
                        </strong>

                        <span>
                            GIORNI
                        </span>
                    </div>


                    <div>
                        <strong data-countdown="hours">
                            00
                        </strong>

                        <span>
                            ORE
                        </span>
                    </div>


                    <div>
                        <strong data-countdown="minutes">
                            00
                        </strong>

                        <span>
                            MIN
                        </span>
                    </div>


                    <div>
                        <strong data-countdown="seconds">
                            00
                        </strong>

                        <span>
                            SEC
                        </span>
                    </div>

                </div>


                <a
                    href="https://wa.me/393714952872?text=${whatsappText}"
                    class="btn btn-primary event-cta"
                    target="_blank"
                    rel="noopener"
                >
                    Info e prenotazioni
                </a>

            </div>
        `;


        /*
         * Imposta subito il countdown
         */
        updateCountdown();
    }


    /*
    ========================================
    STORICO EVENTI
    ========================================
    */

    function renderPastEvents(pastEvents) {

        if (!pastEventsGrid) {
            return;
        }


        if (pastEvents.length === 0) {

            pastEventsGrid.innerHTML = `
                <p class="past-events-empty">
                    Gli eventi passati appariranno qui.
                </p>
            `;

            return;
        }


        pastEventsGrid.innerHTML = pastEvents
            .map(event => {

                return `
                    <article class="past-event-card">

                        <div class="past-event-image">

                            <img
                                src="${event.image}"
                                alt="Locandina ${escapeHtml(event.title)}"
                                loading="lazy"
                            >

                        </div>


                        <div class="past-event-content">

                            <span>
                                ${formatDate(event.dateTime)}
                            </span>

                            <h3>
                                ${escapeHtml(event.title)}
                            </h3>

                        </div>

                    </article>
                `;

            })
            .join("");
    }


    /*
    ========================================
    AGGIORNA SOLO IL COUNTDOWN
    ========================================
    */

    function updateCountdown() {

        if (!currentNextEvent) {
            return;
        }


        const now = new Date();


        /*
         * Se l'evento è appena passato,
         * ricostruiamo la pagina una sola volta
         * per spostarlo nello storico.
         */

        if (currentNextEvent.dateTime <= now) {

            clearInterval(countdownInterval);

            renderPage();

            countdownInterval = setInterval(
                updateCountdown,
                1000
            );

            return;
        }


        const difference =
            currentNextEvent.dateTime - now;


        const totalSeconds =
            Math.floor(difference / 1000);


        const days =
            Math.floor(totalSeconds / 86400);


        const hours =
            Math.floor(
                (totalSeconds % 86400) / 3600
            );


        const minutes =
            Math.floor(
                (totalSeconds % 3600) / 60
            );


        const seconds =
            totalSeconds % 60;


        /*
         * Aggiorniamo SOLO il testo dei numeri.
         * Nessuna ricostruzione delle card.
         */

        const daysElement =
            document.querySelector(
                '[data-countdown="days"]'
            );

        const hoursElement =
            document.querySelector(
                '[data-countdown="hours"]'
            );

        const minutesElement =
            document.querySelector(
                '[data-countdown="minutes"]'
            );

        const secondsElement =
            document.querySelector(
                '[data-countdown="seconds"]'
            );


        if (daysElement) {
            daysElement.textContent =
                String(days).padStart(2, "0");
        }


        if (hoursElement) {
            hoursElement.textContent =
                String(hours).padStart(2, "0");
        }


        if (minutesElement) {
            minutesElement.textContent =
                String(minutes).padStart(2, "0");
        }


        if (secondsElement) {
            secondsElement.textContent =
                String(seconds).padStart(2, "0");
        }
    }


    /*
    ========================================
    DATA ITALIANA
    ========================================
    */

    function formatDate(date) {

        return new Intl.DateTimeFormat(
            "it-IT",
            {
                day: "2-digit",
                month: "2-digit",
                year: "numeric"
            }
        ).format(date);
    }


    /*
    ========================================
    SICUREZZA TESTO
    ========================================
    */

    function escapeHtml(value) {

        return String(value)
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

});
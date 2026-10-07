document.addEventListener("DOMContentLoaded", () => {

    /*
     * Badge "Aperto ora / Chiuso" calcolato sull'ora italiana.
     *
     * Orari: dal martedì alla domenica 17:00 – 02:30, lunedì chiuso.
     * La chiusura alle 02:30 appartiene al giorno precedente:
     * martedì all'01:00 il locale è chiuso (lunedì non apre),
     * lunedì all'01:00 è aperto (coda della domenica).
     *
     * Se cambiano gli orari, modificare solo queste costanti
     * (e il JSON-LD in index.html).
     */

    const OPEN_DAYS = [0, 2, 3, 4, 5, 6];   // 0 = domenica, 1 = lunedì...
    const OPENS = "17:00";
    const CLOSES = "02:30";

    const DAY_NAMES = [
        "domenica", "lunedì", "martedì", "mercoledì",
        "giovedì", "venerdì", "sabato"
    ];

    const badges = document.querySelectorAll("[data-open-status]");

    if (badges.length === 0) {
        return;
    }


    function toMinutes(time) {

        const [hours, minutes] = time.split(":").map(Number);

        return hours * 60 + minutes;
    }


    /*
     * Giorno della settimana e minuti trascorsi
     * dalla mezzanotte, nel fuso orario di Roma.
     */
    function nowInRome() {

        const parts = new Intl.DateTimeFormat("en-US", {
            timeZone: "Europe/Rome",
            weekday: "short",
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23"
        }).formatToParts(new Date());

        const value = type => parts.find(part => part.type === type).value;

        const weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

        return {
            day: weekdays.indexOf(value("weekday")),
            minutes: Number(value("hour")) * 60 + Number(value("minute"))
        };
    }


    function getStatus() {

        const { day, minutes } = nowInRome();

        const opens = toMinutes(OPENS);
        const closes = toMinutes(CLOSES);

        const yesterday = (day + 6) % 7;

        const isOpen =
            (OPEN_DAYS.includes(day) && minutes >= opens) ||
            (OPEN_DAYS.includes(yesterday) && minutes < closes);

        if (isOpen) {
            return {
                open: true,
                text: `Aperto ora · fino alle ${CLOSES}`
            };
        }


        if (OPEN_DAYS.includes(day) && minutes < opens) {
            return {
                open: false,
                text: `Chiuso · apre oggi alle ${OPENS}`
            };
        }


        for (let offset = 1; offset <= 7; offset++) {

            const next = (day + offset) % 7;

            if (OPEN_DAYS.includes(next)) {

                const when = offset === 1
                    ? "domani"
                    : DAY_NAMES[next];

                return {
                    open: false,
                    text: `Chiuso · apre ${when} alle ${OPENS}`
                };
            }
        }

        return { open: false, text: "Chiuso" };
    }


    function render() {

        const status = getStatus();

        badges.forEach(badge => {

            badge.textContent = status.text;

            badge.classList.toggle("is-open", status.open);
            badge.classList.toggle("is-closed", !status.open);

            badge.hidden = false;
        });
    }


    render();

    setInterval(render, 60 * 1000);

});

document.addEventListener("DOMContentLoaded", () => {

    /*
     * Badge "Aperto ora / Chiuso" calcolato sull'ora italiana.
     *
     * Orari: dal martedì alla domenica 17:00 – 02:30, lunedì chiuso.
     * La chiusura dopo mezzanotte appartiene alla serata precedente:
     * martedì all'01:00 il locale è chiuso (lunedì non apre),
     * lunedì all'01:00 è aperto (coda della domenica).
     *
     * Eccezioni per una sola serata in hours.json (le scrive il bot):
     *   { "date": "2026-10-08", "closes": "00:30" }  chiude prima
     *   { "date": "2026-10-08", "closed": true }     serata chiusa
     * "date" è il giorno in cui la serata inizia.
     *
     * Se cambiano gli orari normali, modificare solo queste costanti
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

    let exceptions = {};


    function toMinutes(time) {

        const [hours, minutes] = time.split(":").map(Number);

        return hours * 60 + minutes;
    }


    /*
     * Data (anno, mese, giorno), giorno della settimana e minuti
     * trascorsi dalla mezzanotte, nel fuso orario di Roma.
     */
    function nowInRome() {

        const parts = new Intl.DateTimeFormat("en-US", {
            timeZone: "Europe/Rome",
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23"
        }).formatToParts(new Date());

        const value = type => Number(parts.find(part => part.type === type).value);

        return {
            date: new Date(Date.UTC(value("year"), value("month") - 1, value("day"))),
            minutes: value("hour") * 60 + value("minute")
        };
    }


    function addDays(date, days) {
        return new Date(date.getTime() + days * 86400000);
    }


    function isoDate(date) {
        return date.toISOString().slice(0, 10);
    }


    /*
     * Serata che inizia nel giorno indicato: null se chiusa,
     * altrimenti apertura, chiusura e fine in minuti dalla
     * mezzanotte di quel giorno (oltre 1440 = notte dopo).
     */
    function evening(date) {

        const exception = exceptions[isoDate(date)] || {};

        if (!OPEN_DAYS.includes(date.getUTCDay()) || exception.closed) {
            return null;
        }

        const closes = exception.closes || CLOSES;
        const opens = toMinutes(OPENS);

        let end = toMinutes(closes);

        if (end <= opens) {
            end += 24 * 60;
        }

        return { opens, closes, end, early: Boolean(exception.closes) };
    }


    function nextOpening(today, minutes) {

        const tonight = evening(today);

        if (tonight && minutes < tonight.opens) {
            return "oggi";
        }

        for (let offset = 1; offset <= 7; offset++) {

            const day = addDays(today, offset);

            if (evening(day)) {
                return offset === 1 ? "domani" : DAY_NAMES[day.getUTCDay()];
            }
        }

        return null;
    }


    function getStatus() {

        const { date: today, minutes } = nowInRome();

        const tonight = evening(today);
        const lastNight = evening(addDays(today, -1));

        // Serata di oggi in corso
        if (tonight && minutes >= tonight.opens && minutes < tonight.end) {
            return { open: true, text: `Aperto ora · fino alle ${tonight.closes}` };
        }

        // Coda della serata di ieri, dopo mezzanotte
        if (lastNight && minutes < lastNight.end - 24 * 60) {
            return { open: true, text: `Aperto ora · fino alle ${lastNight.closes}` };
        }

        const when = nextOpening(today, minutes);
        const reopens = when ? ` · apre ${when} alle ${OPENS}` : "";

        // Serata di oggi annullata per eccezione
        if ((exceptions[isoDate(today)] || {}).closed && OPEN_DAYS.includes(today.getUTCDay())) {
            return { open: false, text: `Chiuso stasera${reopens}` };
        }

        return { open: false, text: `Chiuso${reopens}` };
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


    function start() {
        render();
        setInterval(render, 60 * 1000);
    }


    fetch("hours.json", { cache: "no-store" })
        .then(response => response.ok ? response.json() : { exceptions: [] })
        .then(data => {
            (data.exceptions || []).forEach(item => {
                exceptions[item.date] = item;
            });
        })
        .catch(error => console.error("Errore orari:", error))
        .finally(start);

});

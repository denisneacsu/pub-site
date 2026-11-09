document.addEventListener("DOMContentLoaded", () => {

    /*
     * Badge "Aperto ora / Chiuso" calcolato sull'ora italiana.
     *
     * Orario normale in hours.json, "week": 7 voci da lunedì a domenica,
     * { "opens": "19:00", "closes": "02:30" } oppure { "closed": true }.
     * Lo cambia il bot (📅 Orario di apertura). Se manca, valgono
     * le costanti qui sotto: lunedì chiuso, gli altri 19:00 – 02:30.
     * La chiusura dopo mezzanotte appartiene alla serata precedente:
     * martedì all'01:00 il locale è chiuso (lunedì non apre),
     * lunedì all'01:00 è aperto (coda della domenica).
     *
     * Eccezioni per una sola serata in hours.json (le scrive il bot):
     *   { "date": "2026-10-08", "closes": "00:30" }  chiude prima
     *   { "date": "2026-10-08", "closed": true }     serata chiusa
     * "date" è il giorno in cui la serata inizia.
     *
     */

    const OPEN_DAYS = [0, 2, 3, 4, 5, 6];   // 0 = domenica, 1 = lunedì...
    const OPENS = "19:00";
    const CLOSES = "02:30";

    // Orario normale per giorno (0 = domenica): null = chiuso
    let week = [0, 1, 2, 3, 4, 5, 6].map(day =>
        OPEN_DAYS.includes(day) ? { opens: OPENS, closes: CLOSES } : null
    );

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
        const normal = week[date.getUTCDay()];

        if (!normal || exception.closed) {
            return null;
        }

        const closes = exception.closes || normal.closes;
        const opens = toMinutes(normal.opens);

        let end = toMinutes(closes);

        if (end <= opens) {
            end += 24 * 60;
        }

        return { opens, closes, end, starts: normal.opens, early: Boolean(exception.closes) };
    }


    function nextOpening(today, minutes) {

        const tonight = evening(today);

        if (tonight && minutes < tonight.opens) {
            return `oggi alle ${tonight.starts}`;
        }

        for (let offset = 1; offset <= 7; offset++) {

            const day = addDays(today, offset);
            const next = evening(day);

            if (next) {
                return `${offset === 1 ? "domani" : DAY_NAMES[day.getUTCDay()]} alle ${next.starts}`;
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
        const reopens = when ? ` · apre ${when}` : "";

        // Serata di oggi annullata per eccezione
        if ((exceptions[isoDate(today)] || {}).closed && week[today.getUTCDay()]) {
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
            // "week" parte dal lunedì, week[] qui dalla domenica
            if (Array.isArray(data.week) && data.week.length === 7) {
                week = data.week.map((_, day, list) => {
                    const hours = list[(day + 6) % 7];
                    return hours.closed ? null : hours;
                });
            }

            (data.exceptions || []).forEach(item => {
                exceptions[item.date] = item;
            });
        })
        .catch(error => console.error("Errore orari:", error))
        .finally(start);

});

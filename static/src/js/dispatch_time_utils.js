/** Shared 12-hour, stop-local-timezone time formatting for all Dispatch views.
 * Backend always sends UTC ISO strings with a trailing "Z" (see
 * dispatch_job.py::_dt_iso_utc) — this only handles the display side.
 */

/** "2026-07-02T14:30:00Z" + "America/Winnipeg" -> "9:30 AM" */
export function fmtStopTime(isoUtc, tzName) {
    if (!isoUtc) return "";
    try {
        const d = new Date(isoUtc);
        if (isNaN(d.getTime())) return "";
        return new Intl.DateTimeFormat("en-US", {
            hour: "numeric",
            minute: "2-digit",
            hour12: true,
            timeZone: tzName || "America/Toronto",
        }).format(d);
    } catch {
        return "";
    }
}

/** Same as fmtStopTime but also appends the date, e.g. "Jul 2, 9:30 AM" */
export function fmtStopDateTime(isoUtc, tzName) {
    if (!isoUtc) return "";
    try {
        const d = new Date(isoUtc);
        if (isNaN(d.getTime())) return "";
        return new Intl.DateTimeFormat("en-US", {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
            hour12: true,
            timeZone: tzName || "America/Toronto",
        }).format(d);
    } catch {
        return "";
    }
}

/** Parse a loose time input ("9", "9am", "9:30am", "14:30", "2:30 pm")
 * into a 24-hour "HH:MM" string ("09:00"). Empty input or anything
 * unparseable returns "". The caller decides whether empty means "keep
 * current value" or "clear". */
export function parseTimeInputTo24h(raw) {
    if (!raw) return "";
    const s = String(raw).trim().toLowerCase();
    if (!s) return "";
    const m = s.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/);
    if (!m) return "";
    let hour = parseInt(m[1], 10);
    const minute = m[2] ? parseInt(m[2], 10) : 0;
    const meridian = m[3];
    if (minute > 59) return "";
    if (meridian) {
        if (hour < 1 || hour > 12) return "";
        if (meridian === "pm" && hour < 12) hour += 12;
        if (meridian === "am" && hour === 12) hour = 0;
    } else {
        if (hour > 23) return "";
    }
    return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

/** IANA tz name -> short zone abbreviation for the given instant, e.g. "EDT"/"CST". */
export function tzAbbrev(isoUtc, tzName) {
    if (!tzName) return "";
    try {
        const d = isoUtc ? new Date(isoUtc) : new Date();
        const parts = new Intl.DateTimeFormat("en-US", {
            timeZone: tzName,
            timeZoneName: "short",
        }).formatToParts(d);
        const tzPart = parts.find((p) => p.type === "timeZoneName");
        return tzPart ? tzPart.value : "";
    } catch {
        return "";
    }
}

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

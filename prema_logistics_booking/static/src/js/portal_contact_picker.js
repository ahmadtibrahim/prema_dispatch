/* Portal form enhancements for the Service Booking forms.
 *
 * Two independent behaviours. Both are convenience only — every rule they
 * enforce in the browser is re-checked on the server:
 *
 *  1. `.contact-picker` — type-ahead over the logged-in user's OWN private
 *     contacts (`/my/contacts/search`), with inline "create this contact"
 *     (`/my/contacts/quick-create`). Selecting fills the picker's hidden id,
 *     which is what the form actually submits. Typing never creates anything.
 *
 *  2. `.field-help-toggle` — the "?" beside each field. A real <button>, so it
 *     works by click, tap and keyboard (Enter/Space) on every device; never
 *     hover-only.
 *
 * Nothing here reloads the page or touches any other field, so whatever the
 * customer has already typed survives creating or selecting a contact.
 */
(function () {
    "use strict";

    var SEARCH_URL = "/my/contacts/search";
    var CREATE_URL = "/my/contacts/quick-create";
    var DEBOUNCE_MS = 180;

    // ── shared helpers ────────────────────────────────────────────────
    function csrfToken(node) {
        var form = node.closest("form");
        var el = form && form.querySelector("input[name=csrf_token]");
        return el ? el.value : "";
    }

    function escapeHtml(value) {
        var div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    function each(list, fn) {
        Array.prototype.forEach.call(list, fn);
    }

    // ── 1. contact picker ─────────────────────────────────────────────
    function wirePicker(picker) {
        var input = picker.querySelector(".contact-picker-input");
        var hidden = picker.querySelector(".contact-picker-value");
        var results = picker.querySelector(".contact-picker-results");
        var addBtn = picker.querySelector(".contact-picker-add");
        var newBox = picker.querySelector(".contact-picker-new");
        var createBtn = picker.querySelector(".contact-picker-create");
        var errorBox = picker.querySelector(".contact-picker-error");
        if (!input || !hidden || !results) { return; }

        var timer = null;

        function showError(message) {
            input.classList.add("is-invalid");
            if (!errorBox) { return; }
            errorBox.textContent = message;
            errorBox.classList.remove("d-none");
        }

        function clearError() {
            input.classList.remove("is-invalid");
            if (!errorBox) { return; }
            errorBox.textContent = "";
            errorBox.classList.add("d-none");
        }

        function closeResults() {
            results.classList.add("d-none");
            results.innerHTML = "";
        }

        function choose(id, name) {
            input.value = name;
            hidden.value = id;
            clearError();
            closeResults();
        }

        function openNewBox(prefill) {
            if (!newBox) { return; }
            var nameEl = newBox.querySelector(".cp-new-name");
            if (nameEl && prefill) { nameEl.value = prefill; }
            newBox.classList.remove("d-none");
            if (nameEl) { nameEl.focus(); }
        }

        function renderRows(rows, typed) {
            results.innerHTML = "";
            each(rows, function (contact) {
                var item = document.createElement("button");
                item.type = "button";
                item.className = "list-group-item list-group-item-action text-start";
                item.setAttribute("role", "option");
                item.innerHTML = escapeHtml(contact.name) +
                    (contact.phone
                        ? " <span class=\"text-muted\">— " + escapeHtml(contact.phone) + "</span>"
                        : "");
                item.addEventListener("click", function (ev) {
                    ev.preventDefault();
                    choose(contact.id, contact.name);
                });
                results.appendChild(item);
            });

            var trimmed = (typed || "").trim();
            if (!rows.length && trimmed) {
                var create = document.createElement("button");
                create.type = "button";
                create.className = "list-group-item list-group-item-action text-start fw-semibold";
                create.innerHTML = "+ Create &ldquo;" + escapeHtml(trimmed) + "&rdquo;";
                create.addEventListener("click", function (ev) {
                    ev.preventDefault();
                    closeResults();
                    openNewBox(trimmed);
                });
                results.appendChild(create);
            }
            results.classList.toggle("d-none", !results.children.length);
        }

        function search(term) {
            fetch(SEARCH_URL + "?q=" + encodeURIComponent(term), {
                headers: { "Accept": "application/json" },
                credentials: "same-origin"
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (input.value.trim() !== term) { return; }   // stale response
                    renderRows((data && data.results) || [], term);
                })
                .catch(closeResults);
        }

        input.addEventListener("input", function () {
            hidden.value = "";                 // typing invalidates the selection
            clearError();
            var term = input.value.trim();
            if (timer) { window.clearTimeout(timer); }
            if (!term) { closeResults(); return; }
            timer = window.setTimeout(function () {
                if (input.value.trim() === term) { search(term); }
            }, DEBOUNCE_MS);
        });

        input.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape") { closeResults(); }
        });

        input.addEventListener("blur", function () {
            // Let a click on a result land before the list disappears.
            window.setTimeout(closeResults, 200);
        });

        if (addBtn && newBox) {
            addBtn.addEventListener("click", function (ev) {
                ev.preventDefault();
                if (newBox.classList.contains("d-none")) {
                    openNewBox(input.value.trim());
                } else {
                    newBox.classList.add("d-none");
                }
            });
        }

        if (createBtn && newBox) {
            createBtn.addEventListener("click", function (ev) {
                ev.preventDefault();
                var nameEl = newBox.querySelector(".cp-new-name");
                var phoneEl = newBox.querySelector(".cp-new-phone");
                var typeEl = newBox.querySelector(".cp-new-type");
                var name = ((nameEl && nameEl.value) || "").trim();
                if (!name) {
                    if (nameEl) { nameEl.focus(); }
                    showError("Enter a name for the new contact.");
                    return;
                }
                var body = new URLSearchParams();
                body.append("csrf_token", csrfToken(picker));
                body.append("name", name);
                body.append("contact_type", (typeEl && typeEl.value) || "company");
                body.append("phone", ((phoneEl && phoneEl.value) || "").trim());
                createBtn.disabled = true;
                fetch(CREATE_URL, {
                    method: "POST",
                    body: body,
                    headers: { "Accept": "application/json" },
                    credentials: "same-origin"
                })
                    .then(function (r) {
                        if (!r.ok) { throw new Error("HTTP " + r.status); }
                        return r.json();
                    })
                    .then(function (contact) {
                        if (!contact || !contact.id) {
                            throw new Error((contact && contact.error) || "create failed");
                        }
                        choose(contact.id, contact.name);
                        newBox.classList.add("d-none");
                    })
                    .catch(function () {
                        showError("Could not save that contact. Please try again.");
                    })
                    .then(function () { createBtn.disabled = false; });
            });
        }
    }

    // ── 2. "?" field help ─────────────────────────────────────────────
    function wireHelp(toggle) {
        var panel = document.getElementById(toggle.getAttribute("aria-controls"));
        if (!panel) { return; }
        toggle.addEventListener("click", function (ev) {
            ev.preventDefault();
            var open = panel.classList.toggle("d-none") === false;
            toggle.setAttribute("aria-expanded", open ? "true" : "false");
        });
    }

    // ── 3. block a submit that still has unresolved typed text ────────
    // The server applies the same rule; this only saves a round trip.
    function wireSubmitGuard() {
        each(document.querySelectorAll("form.contact-picker-form"), function (form) {
            form.addEventListener("submit", function (ev) {
                var bad = null;
                each(form.querySelectorAll(".contact-picker"), function (picker) {
                    if (bad) { return; }
                    var input = picker.querySelector(".contact-picker-input");
                    var hidden = picker.querySelector(".contact-picker-value");
                    var errorBox = picker.querySelector(".contact-picker-error");
                    var typed = input ? input.value.trim() : "";
                    if (hidden && hidden.value) { return; }
                    if (!typed && picker.getAttribute("data-required") !== "1") { return; }
                    bad = picker;
                    if (input) { input.classList.add("is-invalid"); }
                    if (errorBox) {
                        errorBox.textContent = typed
                            ? "Choose an existing contact below, or create “" + typed +
                              "”, so we know which contact you mean."
                            : "Please name the " +
                              (picker.getAttribute("data-label") || "contact") + ".";
                        errorBox.classList.remove("d-none");
                    }
                });
                if (bad) {
                    ev.preventDefault();
                    var focusInput = bad.querySelector(".contact-picker-input");
                    if (focusInput) { focusInput.focus(); }
                }
            });
        });
    }

    // ── 4. signature pad (booking acceptance) ─────────────────────────
    function wireSignaturePad() {
        var canvas = document.getElementById("accept_signature_pad");
        var hidden = document.getElementById("accept_signature");
        if (!canvas || !hidden) { return; }
        var ctx = canvas.getContext("2d");
        var drawing = false;
        var dirty = false;

        function point(ev) {
            var box = canvas.getBoundingClientRect();
            return {
                x: (ev.clientX - box.left) * (canvas.width / box.width),
                y: (ev.clientY - box.top) * (canvas.height / box.height)
            };
        }

        function start(ev) {
            drawing = true;
            dirty = true;
            var p = point(ev);
            ctx.beginPath();
            ctx.moveTo(p.x, p.y);
            ev.preventDefault();
        }

        function move(ev) {
            if (!drawing) { return; }
            var p = point(ev);
            ctx.lineTo(p.x, p.y);
            ctx.lineWidth = 2.2;
            ctx.lineCap = "round";
            ctx.strokeStyle = "#111";
            ctx.stroke();
            ev.preventDefault();
        }

        function stop() {
            if (!drawing) { return; }
            drawing = false;
            hidden.value = dirty ? canvas.toDataURL("image/png") : "";
        }

        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.lineWidth = 2.2;
        ctx.lineCap = "round";
        ctx.strokeStyle = "#111";

        canvas.addEventListener("pointerdown", start);
        canvas.addEventListener("pointermove", move);
        canvas.addEventListener("pointerup", stop);
        canvas.addEventListener("pointerleave", stop);
        canvas.addEventListener("pointercancel", stop);

        var clear = document.createElement("button");
        clear.type = "button";
        clear.className = "btn btn-sm btn-link px-0 mt-1";
        clear.textContent = "Clear signature";
        clear.addEventListener("click", function () {
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            dirty = false;
            hidden.value = "";
        });
        canvas.parentElement.appendChild(clear);

        // Repaint a signature restored after a rejected submission.
        if (hidden.value && hidden.value.indexOf("data:image") === 0) {
            var img = new Image();
            img.onload = function () { ctx.drawImage(img, 0, 0, canvas.width, canvas.height); };
            img.src = hidden.value;
            dirty = true;
        }
    }

    // ── 5. an appointment is needed once pallets are involved ─────────
    function wireAppointmentRequirement() {
        var pallets = document.getElementById("expected_pallets");
        var arrival = document.getElementById("requested_arrival");
        var departure = document.getElementById("requested_departure");
        if (!pallets || !arrival || !departure) { return; }
        function sync() {
            var needed = parseInt(pallets.value || "0", 10) > 0;
            [arrival, departure].forEach(function (input) {
                if (needed) { input.setAttribute("required", "required"); }
                else { input.removeAttribute("required"); }
            });
        }
        pallets.addEventListener("input", sync);
        pallets.addEventListener("change", sync);
        sync();
    }

    // ── 6. the live estimate ──────────────────────────────────────────
    // A preview only: the server recomputes every amount on submit and never
    // trusts what this sends back. The hidden field exists solely so the
    // server can tell whether the estimate moved while the customer signed.
    function wireEstimate() {
        var table = document.getElementById("pfl_rate_table");
        var form = document.getElementById("service_booking_form");
        if (!table || !form) { return; }
        var rates = [];
        try { rates = JSON.parse(table.dataset.rates || "[]"); } catch (err) { return; }
        var byCode = {};
        rates.forEach(function (r) { byCode[r.code] = r; });

        var empty = document.getElementById("estimate_empty");
        var wrap = document.getElementById("estimate_table_wrap");
        var rows = document.getElementById("estimate_rows");
        var subtotalEl = document.getElementById("estimate_subtotal");
        var taxEl = document.getElementById("estimate_tax");
        var totalEl = document.getElementById("estimate_total");
        var noteEl = document.getElementById("estimate_note");
        var warnEl = document.getElementById("estimate_needs_rate");
        var exclEl = document.getElementById("estimate_exclusive");
        var hidden = document.getElementById("estimate_preview");

        function num(id) {
            var el = document.getElementById(id);
            var value = el ? parseFloat(el.value) : 0;
            return isFinite(value) && value > 0 ? value : 0;
        }
        function money(value) { return (Math.round(value * 100) / 100).toFixed(2); }

        function storageDays() {
            var from = document.getElementById("requested_arrival");
            var to = document.getElementById("requested_departure");
            if (!from || !to || !from.value || !to.value) { return 0; }
            var start = new Date(from.value), end = new Date(to.value);
            if (isNaN(start) || isNaN(end) || end <= start) { return 0; }
            var hours = (end - start) / 3600000;
            if (hours <= 24) { return 0; }              // first 24 h included
            return Math.ceil((hours - 24) / 24);        // started periods only
        }

        function quantity(code) {
            var rate = byCode[code];
            if (!rate) { return 0; }
            switch (rate.qty_source) {
                case "one": return 1;
                case "pallets": return num("expected_pallets");
                case "wrap_qty": return num("wrap_qty");
                case "pallet_supply_qty": return num("pallet_supply_qty");
                case "labour":
                    return Math.max(num("labour_hours"), num("labour_persons"));
                case "storage_days_x_pallets":
                    return storageDays() * num("expected_pallets");
                default: return 1;
            }
        }

        function selected() {
            return Array.prototype.slice
                .call(form.querySelectorAll("input[name=service_code]:checked"))
                .map(function (box) { return box.value; })
                .filter(function (code) { return !!byCode[code]; });
        }

        function recalc() {
            var codes = selected();
            var payload = [];
            var subtotal = 0, tax = 0, needsRate = [], hasStorage = false;
            rows.innerHTML = "";

            codes.forEach(function (code) {
                var rate = byCode[code];
                var qty = quantity(code);
                var line = Math.round(qty * rate.rate * 100) / 100;
                var lineTax = Math.round(line * rate.tax_pct) / 100;
                subtotal += line;
                tax += lineTax;
                if (rate.rate === 0) { needsRate.push(rate.name); }
                if (rate.estimated) { hasStorage = true; }

                var tr = document.createElement("tr");
                tr.innerHTML = "<td>" + rate.name +
                    (rate.estimated ? " <span class=\"text-muted small\">(estimated)</span>" : "") +
                    "</td><td class=\"text-muted small\">" + rate.unit +
                    "</td><td class=\"text-end\">" + qty +
                    "</td><td class=\"text-end\">" + money(rate.rate) +
                    "</td><td class=\"small\">" + rate.tax_label +
                    "</td><td class=\"text-end\">" + money(line + lineTax) + "</td>";
                rows.appendChild(tr);
                payload.push({code: code, quantity: qty, rate: rate.rate,
                              subtotal: line, tax: lineTax, total: line + lineTax});
            });

            var has = codes.length > 0;
            empty.classList.toggle("d-none", has);
            wrap.classList.toggle("d-none", !has);
            subtotalEl.textContent = money(subtotal);
            taxEl.textContent = money(tax);
            totalEl.textContent = money(subtotal + tax);
            noteEl.classList.toggle("d-none", !hasStorage);
            warnEl.classList.toggle("d-none", needsRate.length === 0);
            if (needsRate.length) {
                warnEl.textContent = "These selected services need an agreed rate and stay " +
                    "subject to quotation until our team prices them: " +
                    needsRate.join(", ") + ". Nothing is confirmed at zero.";
            }
            if (hidden) { hidden.value = has ? JSON.stringify(payload) : ""; }
        }

        // LTL and FTL are alternative ways of charging the same job.
        function enforceExclusive(box) {
            var ltl = form.querySelector("input[name=service_code][value=PFL-XD-LTL]");
            var ftl = form.querySelector("input[name=service_code][value=PFL-XD-FTL]");
            if (!ltl || !ftl) { return; }
            var clash = ltl.checked && ftl.checked;
            if (clash && box) {
                if (box.value === ltl.value) { ftl.checked = false; }
                else { ltl.checked = false; }
            }
            exclEl.classList.toggle("d-none", !clash);
        }

        form.addEventListener("change", function (ev) {
            if (ev.target && ev.target.name === "service_code") {
                enforceExclusive(ev.target);
            }
            recalc();
        });
        form.addEventListener("input", recalc);
        recalc();
    }

    function boot() {
        each(document.querySelectorAll(".contact-picker"), wirePicker);
        each(document.querySelectorAll(".field-help-toggle"), wireHelp);
        wireSubmitGuard();
        wireSignaturePad();
        wireAppointmentRequirement();
        wireEstimate();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();

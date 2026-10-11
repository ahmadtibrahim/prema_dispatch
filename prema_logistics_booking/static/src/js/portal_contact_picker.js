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

    function boot() {
        each(document.querySelectorAll(".contact-picker"), wirePicker);
        each(document.querySelectorAll(".field-help-toggle"), wireHelp);
        wireSubmitGuard();
        wireSignaturePad();
        wireAppointmentRequirement();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();

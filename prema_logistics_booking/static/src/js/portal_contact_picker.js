/* Searchable contact picker for the portal booking form.
 *
 * Suggestions come from /my/contacts/search and new contacts from
 * /my/contacts/quick-create — both scoped to the logged-in user on the
 * server, so this script is convenience only, never the access control.
 * Everything already typed into the form is left untouched: selecting or
 * creating a contact only fills this picker's two inputs.
 */
(function () {
    "use strict";

    function csrfToken(picker) {
        var form = picker.closest("form");
        var el = form && form.querySelector("input[name=csrf_token]");
        return el ? el.value : "";
    }

    function wire(picker) {
        var input = picker.querySelector(".contact-picker-input");
        var hidden = picker.querySelector("input[type=hidden]");
        var results = picker.querySelector(".contact-picker-results");
        var addBtn = picker.querySelector(".contact-picker-add");
        var newBox = picker.querySelector(".contact-picker-new");
        var createBtn = picker.querySelector(".contact-picker-create");
        if (!input || !hidden || !results) { return; }

        function close() {
            results.classList.add("d-none");
            results.innerHTML = "";
        }

        function choose(id, name) {
            input.value = name;
            hidden.value = id;
            close();
        }

        input.addEventListener("input", function () {
            hidden.value = "";               // typing invalidates the selection
            var q = input.value.trim();
            if (!q) { close(); return; }
            fetch("/my/contacts/search?q=" + encodeURIComponent(q))
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    var rows = (data && data.results) || [];
                    results.innerHTML = "";
                    rows.forEach(function (contact) {
                        var item = document.createElement("a");
                        item.href = "#";
                        item.className = "list-group-item list-group-item-action";
                        item.textContent = contact.name + (contact.phone ? " — " + contact.phone : "");
                        item.addEventListener("click", function (ev) {
                            ev.preventDefault();
                            choose(contact.id, contact.name);
                        });
                        results.appendChild(item);
                    });
                    results.classList.toggle("d-none", rows.length === 0);
                })
                .catch(close);
        });

        if (addBtn) {
            addBtn.addEventListener("click", function () {
                newBox.classList.toggle("d-none");
            });
        }

        if (createBtn) {
            createBtn.addEventListener("click", function () {
                var nameEl = picker.querySelector(".cp-new-name");
                var phoneEl = picker.querySelector(".cp-new-phone");
                var name = (nameEl.value || "").trim();
                if (!name) { nameEl.focus(); return; }
                var body = new URLSearchParams();
                body.append("csrf_token", csrfToken(picker));
                body.append("name", name);
                body.append("contact_type", "company");
                body.append("phone", (phoneEl.value || "").trim());
                fetch("/my/contacts/quick-create", { method: "POST", body: body })
                    .then(function (r) { return r.json(); })
                    .then(function (contact) {
                        if (contact && contact.id) {
                            choose(contact.id, contact.name);
                            newBox.classList.add("d-none");
                        }
                    });
            });
        }

        document.addEventListener("click", function (ev) {
            if (!picker.contains(ev.target)) { close(); }
        });
    }

    function boot() {
        document.querySelectorAll(".contact-picker").forEach(wire);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();

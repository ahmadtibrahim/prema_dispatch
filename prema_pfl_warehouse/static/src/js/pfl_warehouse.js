/** @odoo-module **/
/*
 * PF Logistics warehouse phone screen.
 *
 * Plain DOM, no framework: this page has to work on whatever phone is in
 * someone's pocket, and it has to keep working when the asset bundle is stale.
 * The only interaction that matters is the scan box, which accepts a
 * keyboard-wedge scanner, a typed code, or a customer barcode.
 */
(function () {
    "use strict";

    var current = null;
    var photoData = null;

    function rpc(url, params) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                jsonrpc: "2.0",
                method: "call",
                params: params || {}
            })
        }).then(function (response) {
            return response.json();
        }).then(function (data) {
            if (data.error) {
                var info = data.error.data || {};
                throw new Error(info.message || data.error.message);
            }
            return data.result;
        });
    }

    function el(id) {
        return document.getElementById(id);
    }

    function show(kind, text) {
        var box = el("pfl_message");
        box.className = "alert alert-" + kind;
        box.textContent = text;
    }

    function clearMessage() {
        el("pfl_message").className = "alert d-none";
    }

    function render(pkg) {
        current = pkg;
        el("pfl_card").className = "card mb-3";
        el("pfl_actions").className = "";
        el("pfl_pallet_name").textContent = pkg.name;
        el("pfl_pallet_state").textContent = pkg.state;
        el("pfl_job").textContent = pkg.job ? pkg.job.name : "—";
        el("pfl_customer").textContent = pkg.job ? pkg.job.customer : "—";
        el("pfl_owner").textContent = pkg.owner || "—";
        el("pfl_location").textContent = pkg.location || "—";
        el("pfl_received").textContent = pkg.received_at || "—";
        el("pfl_released").textContent = pkg.released_at || "—";
        el("pfl_hours").textContent = pkg.custody_hours + " h";
        el("pfl_label_mode").textContent =
            pkg.label_mode === "no_attached_label"
                ? "Off-pallet card: " + (pkg.card || "not yet assigned")
                : "Label attached";

        var holdBox = el("pfl_hold_box");
        if (pkg.on_hold) {
            holdBox.className = "alert alert-warning py-2";
            el("pfl_hold_reason").textContent = pkg.hold_reason || "no reason recorded";
        } else {
            holdBox.className = "alert alert-warning d-none py-2";
        }
    }

    function lookup() {
        var code = el("pfl_scan").value.trim();
        if (!code) {
            return;
        }
        clearMessage();
        rpc("/pfl/api/resolve", { code: code }).then(function (res) {
            if (res.status === "ok") {
                render(res.package);
            } else {
                show("warning", res.message || "No pallet found for that code.");
            }
        }).catch(function (error) {
            show("danger", error.message);
        });
    }

    function act(name) {
        if (!current) {
            return;
        }
        var fileInput = el("pfl_photo");
        rpc("/pfl/api/action", {
            package_ids: [current.id],
            action: name,
            dest_barcode: el("pfl_dest").value,
            notes: el("pfl_notes").value,
            photo: photoData,
            photo_name: fileInput.files[0] ? fileInput.files[0].name : null
        }).then(function (res) {
            var message = name.replace(/_/g, " ") + " recorded.";
            if (res.warning) {
                message += " " + res.warning;
            }
            show("success", message);
            if (res.packages && res.packages.length) {
                render(res.packages[0]);
            }
            photoData = null;
            el("pfl_photo_state").textContent = "";
        }).catch(function (error) {
            show("danger", error.message);
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        var scan = el("pfl_scan");
        if (!scan) {
            return;
        }
        el("pfl_resolve").addEventListener("click", lookup);
        scan.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                lookup();
            }
        });
        Array.prototype.forEach.call(
            document.querySelectorAll("#pfl_actions button[data-act]"),
            function (button) {
                button.addEventListener("click", function () {
                    act(button.getAttribute("data-act"));
                });
            });
        el("pfl_print").addEventListener("click", function () {
            if (current) {
                window.open(
                    "/report/pdf/prema_pfl_warehouse.action_report_pfl_pallet_label/"
                    + current.id, "_blank");
            }
        });
        el("pfl_photo").addEventListener("change", function (event) {
            var file = event.target.files[0];
            if (!file) {
                photoData = null;
                return;
            }
            var reader = new FileReader();
            reader.onload = function () {
                photoData = reader.result;
                el("pfl_photo_state").textContent = "Photo attached: " + file.name;
            };
            reader.readAsDataURL(file);
        });
    });
})();

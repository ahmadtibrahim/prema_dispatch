/** @odoo-module **/
/* PF Logistics inside the native Barcode app.
 *
 * Small extensions to stock_barcode, not a second application:
 *  - two label buttons in Barcode's own Print section, on a PFL receipt only;
 *  - an "Arrived" and a "Prepare Pallets" action in Barcode's actions view.
 *
 * Everything they call already exists on stock.picking, so the employee never
 * has to open a backend form or Sales to do routine work.
 */
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import BarcodePickingModel from "@stock_barcode/models/barcode_picking_model";

patch(BarcodePickingModel.prototype, {
    get printButtons() {
        const buttons = super.printButtons;
        const record = this.record || {};
        if (record.pfl_is_warehouse_receipt) {
            buttons.push({
                name: _t("Print Pallet Labels"),
                class: "o_print_pfl_labels btn-primary",
                method: "action_pfl_print_labels",
            }, {
                name: _t("Print Pallet Cards"),
                class: "o_print_pfl_cards",
                method: "action_pfl_print_cards",
            });
        }
        return buttons;
    },

    /** Run a PFL dock action and tell the employee what happened. */
    async pflAction(method) {
        await this.save();
        try {
            const result = await this.orm.call(this.resModel, method, [[this.resId]]);
            const params = result && result.params;
            if (params && params.message) {
                this.notification(params.message, { type: params.type || "info" });
            }
        } catch (error) {
            this.notification(error.message || _t("That action failed."),
                              { type: "danger" });
        }
    },
});

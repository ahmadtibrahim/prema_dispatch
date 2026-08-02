/** @odoo-module */
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";

class WhereWeGoAction extends Component {
    static template = "prema_logistics_booking.WhereWeGoAction";
    static props = {};
}
registry.category("actions").add("prema_where_we_go", WhereWeGoAction);

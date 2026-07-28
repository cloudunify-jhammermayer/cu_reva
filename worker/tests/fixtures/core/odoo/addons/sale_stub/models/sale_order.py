from odoo import api, fields, models


class SaleOrder(models.Model):
    _name = "sale.order"
    _description = "Sales Order"

    partner_id = fields.Many2one("res.partner", string="Customer")
    amount_total = fields.Monetary(string="Total", compute="_compute_amounts")
    company_currency = fields.Many2one(related="company_id.currency_id")
    note = fields.Text()


class SaleOrderLine(models.Model):
    _name = "sale.order.line"
    _inherit = ["analytic.mixin"]
    _description = "Sales Order Line"

    order_id = fields.Many2one("sale.order")
    # Mirrors the real Odoo 19 sale_management field. Named nowhere in the
    # module summary or model description, so it is only reachable if
    # search_registry actually queries fields (ticket 6743).
    is_optional = fields.Boolean(string="Optional Line")


class ResPartner(models.Model):
    _inherit = "res.partner"

    sale_order_count = fields.Integer(compute="_compute_sale_order_count")

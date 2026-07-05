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


class ResPartner(models.Model):
    _inherit = "res.partner"

    sale_order_count = fields.Integer(compute="_compute_sale_order_count")

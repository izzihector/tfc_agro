# -*- coding: utf-8 -*-
from num2words import num2words
from odoo import models, fields, api, _
from odoo.addons import decimal_precision as dp
from odoo.exceptions import UserError, ValidationError

from datetime import datetime

class SaleOrder(models.Model):
    _inherit='sale.order'  
    
    def _num_to_words(self, num):
        def _num2words(number, lang):
            try:
                return num2words(number, lang=lang).title()
            except NotImplementedError:
                return num2words(number, lang='en').title()
        if num2words is None:
            logging.getLogger(__name__).warning("The library 'num2words' is missing, cannot render textual amounts.")
            return ""
        lang_code = self.env.context.get('lang') or self.env.user.lang
        lang = self.env['res.lang'].with_context(active_test=False).search([('code', '=', lang_code)])
        num_to_word = _num2words(num, lang=lang.iso_code)
        return num_to_word
    
    @api.depends('partner_invoice_id.credit', 'partner_invoice_id.credit_limit')
    def compute_over_credit(self):
        for record in self:
            record[('over_credit')] = record.partner_invoice_id.credit > record.partner_invoice_id.credit_limit
    
    @api.one
    @api.depends('order_line.product_uom_qty')
    def _compute_total_qty(self):
        self.sum_qty = sum(line.product_uom_qty for line in self.order_line)
    
    @api.multi
    def _compute_num_to_words(self):
        for rec in self:
            rec.qty_to_text = str(self._num_to_words(rec.sum_qty))
    @api.multi
    def _compute_amount_in_word(self):
        for rec in self:
            rec.amount_to_text = str(rec.currency_id.amount_to_text(rec.amount_total)) #+ ' only'
    
    vehicle_number=fields.Char(string='Vehicle Number')
    driver_name=fields.Char(string="Driver Name")
    driver_contacts=fields.Char(string="Driver Contact")
    customer_order_ref=fields.Char(string="Customer Order Ref")
    sale_approver=fields.Many2one('res.users', string="Approver")
    over_credit = fields.Boolean(string="Over Credit", store=True, readonly=True, compute=compute_over_credit)
    sum_qty = fields.Float(string="Total Qty", compute='_compute_total_qty')            
    qty_to_text = fields.Char(string="Qty In Words", compute='_compute_num_to_words')
    amount_to_text = fields.Char(string="Amount In Words:", compute='_compute_amount_in_word')
    partner_total_due = fields.Monetary(string='Total Due:', related="partner_invoice_id.total_due")
    
    @api.model
    def get_move_from_line(self, line):
        move = self.env['stock.move']
        # i create this counter to check lot's univocity on move line
        lot_count = 0
        for p in line.order_id.picking_ids:
            for m in p.move_lines:
                move_line_id = m.move_line_ids.filtered(
                    lambda line: line.lot_id)
                if move_line_id and line.lot_id == move_line_id[:1].lot_id:
                    move = m
                    lot_count += 1
                    # if counter is 0 or > 1 means that something goes wrong
                    if lot_count != 1:
                        raise UserError(_('Can\'t retrieve lot on stock'))
        return move

    @api.model
    def _check_move_state(self, line):
        if line.lot_id:
            move = self.get_move_from_line(line)
            if move.state == 'confirmed':
                move._action_assign()
                move.refresh()
            if move.state != 'assigned':
                raise UserError(_('Can\'t reserve products for lot %s') %
                                line.lot_id.name)
        return True

    @api.multi
    def action_confirm(self):
        res = super(SaleOrder, self.with_context(sol_lot_id=True))\
            .action_confirm()
        for line in self.order_line:
            if line.lot_id:
                unreserved_moves = line.move_ids.filtered(
                    lambda move: move.product_uom_qty !=
                    move.reserved_availability
                )
                if unreserved_moves:
                    raise UserError(
                        _('Can\'t reserve products for lot %s')
                        % line.lot_id.name
                    )
            self._check_move_state(line)
        return res    
    
    #Inherit create method to add custom sequence in sale order
    @api.model
    def create(self, vals):
        if vals.get('name', _('New')) == _('New'):
            if 'company_id' in vals:
                vals['name'] = self.env['ir.sequence'].with_context(force_company=vals['company_id']).next_by_code('sale.bov.sequence') or _('New')
            else:
                vals['name'] = self.env['ir.sequence'].next_by_code('sale.adl.sequence') or _('New')

        # Makes sure partner_invoice_id', 'partner_shipping_id' and 'pricelist_id' are defined
        if any(f not in vals for f in ['partner_invoice_id', 'partner_shipping_id', 'pricelist_id']):
            partner = self.env['res.partner'].browse(vals.get('partner_id'))
            addr = partner.address_get(['delivery', 'invoice'])
            vals['partner_invoice_id'] = vals.setdefault('partner_invoice_id', addr['invoice'])
            vals['partner_shipping_id'] = vals.setdefault('partner_shipping_id', addr['delivery'])
            vals['pricelist_id'] = vals.setdefault('pricelist_id', partner.property_product_pricelist and partner.property_product_pricelist.id)
        result = super(SaleOrder, self).create(vals)
        return result
    
    #Override write method to change BOV to ADL when status change
    @api.multi
    def action_confirm(self):
        if super(SaleOrder, self).action_confirm():
            for order in self:
                if order.state == 'sale':
                    if order.origin and order.origin != '':
                        quo = order.origin + ', ' + order.name
                    else:
                        quo = order.name
                    order.write({
                        'origin': quo,
                        'name': self.env['ir.sequence'].next_by_code('sale.adl.sequence')
                    })
        return True
    
class SaleOrderLine(models.Model):
    _inherit='sale.order.line'
    
    lot_id=fields.Many2one('stock.production.lot', string='Lot', copy=False)
    lot_quantity=fields.Float(string="Quantity in Lot", related='lot_id.product_qty', default=1.00, 
                              required=True, digits=dp.get_precision('Product Unit of Measure')
    )    
    
    @api.one
    @api.constrains('lot_id', 'product_uom_qty')
    def _compare_lot_qty(self):
        #for line
        if self.lot_id:
            if self.lot_id.product_qty < self.product_uom_qty:
                raise ValidationError("There is not enough product %s in Lot %s" % (self.product_id.name, self.lot_id.name))

    @api.onchange('product_id')
    def _onchange_product_id_set_lot_domain(self):
        available_lot_ids=[] #On itialise la liste des lots disponible
            
        #Si le produit existe et il existe un entrepot pour le devis
        if self.product_id and self.order_id.warehouse_id:
            #Je recupere le nom du stock
            location = self.order_id.warehouse_id.lot_stock_id
            product_quantity = self.lot_quantity#product qty in sale order line
            quants = self.env['stock.quant'].read_group([
                ('product_id', '=', self.product_id.id),
                ('location_id', 'child_of', location.id),
                ('quantity', '>', 0),
                ('lot_id', '!=', False)
                ], ['lot_id'], 'lot_id')           
            available_lot_ids = [quant['lot_id'][0] for quant in quants]
        self.lot_id = False
        return {
            'domain':{'lot_id':[('id', 'in', available_lot_ids)]}
        }
    #When choose one lot we compare qty in lot and orderd qty
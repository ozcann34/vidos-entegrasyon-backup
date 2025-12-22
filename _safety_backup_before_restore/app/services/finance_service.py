"""
Finansal Analiz Servisi
Sipariş maliyetleri, kesintiler, net kâr ve ROI hesaplamalarını yönetir.
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from app import db
from app.models import Order, Product, Expense

class ProfitCalculator:
    """Verilen sipariş için kârlılık hesaplar."""
    
    @staticmethod
    def calculate_order_profit(order: Order, update_db: bool = False) -> Dict[str, float]:
        """
        Bir siparişin net kârını hesaplar.
        Formül: Toplam Satış - (KDV + Komisyon + Kargo + Hizmet Bedeli + Ürün Maliyeti)
        """
        import json
        
        # 1. Gelirler
        gross_sales = order.total_price or 0.0
        marketplace_discount = order.marketplace_discount or 0.0
        
        # 2. Vergi (KDV)
        # Sipariş bazlı KDV oranı (Varsayılan %20)
        vat_rate_perc = order.vat_rate if (order.vat_rate is not None) else 20.0
        vat_rate = vat_rate_perc / 100.0
        
        # Matrah hesaplanırken satıcı indirimi düşülür (KDV dahil matrah üzerinden)
        # Net Satış (KDV hariç) = (Brüt - İndirim) / (1 + KDV)
        base_for_vat = gross_sales - marketplace_discount
        net_sales = base_for_vat / (1 + vat_rate)
        vat_amount = base_for_vat - net_sales
        
        # 3. Giderler (Pazaryeri Kesintileri)
        commission = order.commission_amount or 0.0
        shipping = order.shipping_fee or 0.0
        service = order.service_fee or 0.0
        total_deductions = commission + shipping + service
        
        # 4. Ürün Maliyetleri (XML-Matched priority in summary, here we check local DB)
        total_cog = 0.0 # Cost of Goods
        
        try:
            items = json.loads(order.items_json) if order.items_json else []
            for item in items:
                barcode = item.get('barcode')
                qty = float(item.get('quantity', 1))
                
                product = None
                if barcode:
                    product = Product.query.filter_by(barcode=barcode, user_id=order.user_id).first()
                
                if product and product.cost_price:
                    total_cog += product.cost_price * qty
        except Exception as e:
            print(f"Cost calc error for order {order.id}: {e}")
            
        # 5. Net Kâr (KDV Hariç Gelir - Kesintiler - Ürün Maliyeti)
        net_profit = net_sales - total_deductions - total_cog
        
        total_investment = total_cog + total_deductions
        roi = (net_profit / total_investment * 100) if total_investment > 0 else 0.0
        
        result = {
            'gross_sales': gross_sales,
            'net_sales': net_sales,
            'vat_amount': vat_amount,
            'total_deductions': total_deductions,
            'total_cost': total_cog,
            'net_profit': net_profit,
            'roi': roi,
            'margin': (net_profit / net_sales * 100) if net_sales > 0 else 0.0
        }
        
        if update_db:
            order.total_deductions = total_deductions
            order.net_profit = net_profit
            order.tax_amount = vat_amount
            db.session.commit()
            
        return result

def get_financial_summary(user_id: int, start_date: datetime = None, end_date: datetime = None) -> Dict[str, Any]:
    """Tarih aralığına göre finansal özet raporu döner."""
    from app.services.xml_service import load_xml_source_index, lookup_xml_record
    from app.models import SupplierXML
    
    # 1. Load XML Cost Map (Live override)
    xml_sources = SupplierXML.query.filter_by(user_id=user_id).all()
    xml_indices = []
    for s in xml_sources:
        try:
            xml_indices.append(load_xml_source_index(s.id))
        except:
            continue

    # Filter out cancelled orders (handling common marketplace status strings)
    # We'll use these for loss calculation
    cancelled_statuses = ['cancelled', 'Cancelled', 'İptal Edildi', 'İptal', 'İade Edildi', 'Returned', 'Rejected']
    
    # Base Query
    base_query = Order.query.filter_by(user_id=user_id)
    if start_date:
        base_query = base_query.filter(Order.created_at >= start_date)
    if end_date:
        base_query = base_query.filter(Order.created_at <= end_date)
        
    all_orders = base_query.all()
    
    orders = [] # Active orders
    cancelled_orders = []
    
    for o in all_orders:
        if o.status in cancelled_statuses or any(cs.lower() in (o.status or "").lower() for cs in ['iptal', 'cancel', 'iade', 'return']):
            cancelled_orders.append(o)
        else:
            orders.append(o)
            
    total_revenue = 0.0
    total_net_profit = 0.0
    total_shipping = 0.0
    total_commission = 0.0
    total_cost = 0.0
    total_tax = 0.0
    
    marketplace_stats = {
        mp: {
            'name': mp, 'revenue': 0.0, 'count': 0, 'commission': 0.0, 
            'shipping': 0.0, 'tax': 0.0, 'cost': 0.0, 'profit': 0.0, 'discount': 0.0
        } for mp in ['trendyol', 'hepsiburada', 'n11', 'pazarama', 'idefix']
    }
    for order in orders:
        mp = (order.marketplace or 'bilinmeyen').lower()
        if mp not in marketplace_stats:
            marketplace_stats[mp] = {
                'name': mp, 'revenue': 0.0, 'count': 0, 'commission': 0.0, 
                'shipping': 0.0, 'tax': 0.0, 'cost': 0.0, 'profit': 0.0, 'discount': 0.0
            }
        
        # Calculate/Get Cost (XML priority)
        order_cost = 0.0
        import json
        try:
            items = json.loads(order.items_json) if order.items_json else []
            for item in items:
                barcode = item.get('barcode')
                qty = float(item.get('quantity', 1))
                
                # Check XML index for cost
                item_cost = 0.0
                for idx in xml_indices:
                    match = lookup_xml_record(idx, code=barcode, stock_code=item.get('stockCode'))
                    if match and match.get('cost') and match['cost'] > 0:
                        item_cost = match['cost']
                        break
                
                if item_cost == 0:
                    # Fallback to local product
                    product = Product.query.filter_by(barcode=barcode, user_id=user_id).first()
                    if product and product.cost_price:
                        item_cost = product.cost_price
                
                if item_cost > 0:
                    order_cost += item_cost * qty
                else:
                    # Final fallback: estimated 70% of unit price if cost is totally missing
                    order_cost += (item.get('price', 0) / qty) * 0.70 * qty
                    
        except Exception as e:
            # Fallback for old calculation
             profit = order.net_profit or 0.0
             order_cost = (order.total_price / 1.20) - profit - (order.total_deductions or 0)
        
        order_net_revenue = (order.total_price or 0.0) - (order.marketplace_discount or 0.0)
        order_tax = order_net_revenue - (order_net_revenue / (1 + (order.vat_rate or 20.0)/100.0))
        
        order_net_profit = (order_net_revenue / (1 + (order.vat_rate or 20.0)/100.0)) - (order.commission_amount or 0) - (order.shipping_fee or 0) - (order.service_fee or 0) - order_cost
        
        total_revenue += order.total_price or 0
        total_net_profit += order_net_profit
        total_shipping += order.shipping_fee or 0
        total_commission += order.commission_amount or 0
        total_cost += max(0, order_cost)
        total_tax += order_tax
        
        marketplace_stats[mp]['revenue'] += order.total_price or 0
        marketplace_stats[mp]['count'] += 1
        marketplace_stats[mp]['commission'] += order.commission_amount or 0
        marketplace_stats[mp]['shipping'] += order.shipping_fee or 0
        marketplace_stats[mp]['tax'] += order_tax
        marketplace_stats[mp]['cost'] += max(0, order_cost)
        marketplace_stats[mp]['profit'] += order_net_profit
        marketplace_stats[mp]['discount'] += order.marketplace_discount or 0
    
    # Process Cancelled Orders for loss
    cancelled_loss = sum(o.total_price for o in cancelled_orders)
    cancelled_count = len(cancelled_orders)
    
    # Format marketplace breakdown for template
    mp_breakdown = []
    for mp, data in marketplace_stats.items():
        if data['count'] > 0: # Only show marketplaces with activity
            mp_breakdown.append(data)

    # Genel Giderleri de hesaba kat (Tarih bazlı)
    expense_query = Expense.query.filter_by(user_id=user_id)
    if start_date:
        expense_query = expense_query.filter(Expense.date >= start_date.date())
    if end_date:
        expense_query = expense_query.filter(Expense.date <= end_date.date())
    
    expenses_list = expense_query.all()
    total_general_expenses = sum(e.amount for e in expenses_list)
    
    final_net_profit = total_net_profit - total_general_expenses
    
    return {
        'revenue': total_revenue,
        'gross_profit': total_net_profit, # Satış kârı
        'general_expenses': total_general_expenses,
        'net_profit': final_net_profit, # Gerçek net kâr
        'net_margin': (final_net_profit / total_revenue * 100) if total_revenue > 0 else 0,
        'total_shipping': total_shipping,
        'total_commission': total_commission,
        'total_product_cost': total_cost,
        'total_tax': total_tax,
        'order_count': len(orders),
        'mp_breakdown': mp_breakdown,
        'cancelled_count': cancelled_count,
        'cancelled_loss': cancelled_loss
    }

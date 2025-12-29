"""
Finansal Analiz Servisi
Sipariş maliyetleri, kesintiler, net kâr ve ROI hesaplamalarını yönetir.
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from app import db
from app.models import Order, Product

class ProfitCalculator:
    """Verilen sipariş için kârlılık hesaplar."""
    
    @staticmethod
    def calculate_order_profit(order: Order, update_db: bool = False) -> Dict[str, float]:
        """
        Bir siparişin net kârını hesaplar.
        Formül: Toplam Satış - (Toplam Maliyet + Komisyon + Kargo + Hizmet Bedeli)
        """
        import json
        
        # 1. Gelirler
        gross_sales = order.total_price or 0.0
        
        # 2. Giderler (Pazaryeri Kesintileri)
        commission = order.commission_amount or 0.0
        shipping = order.shipping_fee or 0.0
        service = order.service_fee or 0.0
        total_deductions = commission + shipping + service
        
        # 3. Ürün Maliyetleri
        total_cog = 0.0 # Cost of Goods
        
        try:
            items = json.loads(order.items_json) if order.items_json else []
            for item in items:
                # Barkod veya stok koduna göre ürünü bul
                barcode = item.get('barcode')
                qty = float(item.get('quantity', 1))
                
                product = None
                if barcode:
                    product = Product.query.filter_by(barcode=barcode, user_id=order.user_id).first()
                
                if product and product.cost_price:
                    total_cog += product.cost_price * qty
                else:
                    # Maliyet girilmemişse, tahmini bir maliyet varsayabiliriz (Örn: Satışın %40'ı)
                    # Ancak şimdilik 0 kabul edip raporlarda "Maliyet Girilmemiş" uyarısı göstermek daha doğru.
                    pass
                    
        except Exception as e:
            print(f"Cost calc error for order {order.id}: {e}")
            
        # 4. Net Kâr
        net_profit = gross_sales - total_deductions - total_cog
        
        # 5. ROI (Return on Investment)
        # ROI = (Net Kâr / Toplam Maliyet) * 100
        total_investment = total_cog + total_deductions
        roi = (net_profit / total_investment * 100) if total_investment > 0 else 0.0
        
        result = {
            'gross_sales': gross_sales,
            'total_deductions': total_deductions,
            'total_cost': total_cog,
            'net_profit': net_profit,
            'roi': roi,
            'margin': (net_profit / gross_sales * 100) if gross_sales > 0 else 0.0
        }
        
        if update_db:
            order.total_deductions = total_deductions
            order.net_profit = net_profit
            db.session.commit()
            
        return result

def get_financial_summary(user_id: int, start_date: datetime = None, end_date: datetime = None) -> Dict[str, Any]:
    """Tarih aralığına göre finansal özet raporu döner."""
    
    query = Order.query.filter_by(user_id=user_id).filter(
        ~Order.status.ilike('%iptal%'),
        ~Order.status.ilike('%iade%'),
        ~Order.status.ilike('%cancel%'),
        ~Order.status.ilike('%return%'),
        ~Order.status.ilike('%red%'),
        Order.status != 'REJECTED'
    )
    
    if start_date:
        query = query.filter(Order.order_date >= start_date)
    if end_date:
        query = query.filter(Order.order_date <= end_date)
        
    orders = query.all()
    
    total_revenue = 0.0
    total_profit = 0.0
    total_shipping = 0.0
    total_commission = 0.0
    total_cost = 0.0
    
    for order in orders:
        # Eğer kar hesaplanmamışsa hesapla
        if order.net_profit == 0 and order.total_price > 0:
             calc = ProfitCalculator.calculate_order_profit(order, update_db=True)
             profit = calc['net_profit']
             cost = calc['total_cost']
        else:
             profit = order.net_profit
             # Maliyet verisi order tablosunda saklanmıyor, tekrar hesaplamak lazım veya yaklaşık bulmak
             # Basitlik için: Satış - Kâr - Kesintiler = Maliyet
             cost = order.total_price - profit - (order.total_deductions or 0)
        
        total_revenue += order.total_price or 0
        total_profit += profit
        total_shipping += order.shipping_fee or 0
        total_commission += order.commission_amount or 0
        total_cost += cost
    
    return {
        'revenue': total_revenue,
        'gross_profit': total_profit, # Net kar aslında
        'net_margin': (total_profit / total_revenue * 100) if total_revenue > 0 else 0,
        'total_shipping': total_shipping,
        'total_commission': total_commission,
        'total_product_cost': total_cost,
        'order_count': len(orders)
    }

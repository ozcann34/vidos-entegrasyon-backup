import random
import logging
from datetime import datetime
from app import db
from app.models import Product, MarketplaceProduct
from app.services.activity_logger import log_user_activity

def generate_ean13():
    """Generate a valid-looking 13 digit random barcode starting with 868 (Turkey)"""
    prefix = "868"
    body = "".join([str(random.randint(0, 9)) for _ in range(9)])
    temp = prefix + body
    
    # Checksum calculation (Mod 10)
    total = 0
    for i in range(12):
        total += int(temp[i]) * (1 if i % 2 == 0 else 3)
    check_digit = (10 - (total % 10)) % 10
    return temp + str(check_digit)

def bulk_generate_missing_barcodes(user_id):
    """Generate barcodes for products that have none or are too short/invalid"""
    products = Product.query.filter_by(user_id=user_id).filter(
        (Product.barcode == None) | (Product.barcode == '') | (db.func.length(Product.barcode) < 5)
    ).all()
    
    count = 0
    updated_barcodes = []
    
    for p in products:
        old_barcode = p.barcode
        new_barcode = generate_ean13()
        # Ensure uniqueness in local scope (simple check)
        while Product.query.filter_by(user_id=user_id, barcode=new_barcode).first():
            new_barcode = generate_ean13()
            
        p.barcode = new_barcode
        updated_barcodes.append({'id': p.id, 'title': p.title, 'old': old_barcode, 'new': new_barcode})
        count += 1
        
    db.session.commit()
    log_user_activity(user_id, 'bulk_barcode_generate', 'system', {'count': count})
    return {'success': True, 'count': count, 'details': updated_barcodes}

def bulk_override_all_barcodes(user_id):
    """Override ALL product barcodes with random ones (DANGER)"""
    products = Product.query.filter_by(user_id=user_id).all()
    
    count = 0
    updated_barcodes = []
    
    for p in products:
        old_barcode = p.barcode
        new_barcode = generate_ean13()
        while Product.query.filter_by(user_id=user_id, barcode=new_barcode).first():
            new_barcode = generate_ean13()
            
        p.barcode = new_barcode
        updated_barcodes.append({'id': p.id, 'title': p.title, 'old': old_barcode, 'new': new_barcode})
        count += 1
        
    db.session.commit()
    log_user_activity(user_id, 'bulk_barcode_override', 'system', {'count': count})
    return {'success': True, 'count': count, 'details': updated_barcodes}

def generate_barcode_report(details):
    """Nicely format the barcode update details for the UI or logs"""
    report = f"Barkod Güncelleme Raporu - {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
    report += "-"*50 + "\n"
    for item in details:
        report += f"ID: {item['id']} | {item['title'][:40]:<40} | {item['old']} -> {item['new']}\n"
    return report

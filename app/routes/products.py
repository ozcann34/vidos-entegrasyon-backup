from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app
from flask_login import login_required, current_user
import pandas as pd
import os
from werkzeug.utils import secure_filename
from app import db
from app.models import Product, AdminLog

products_bp = Blueprint('products', __name__, url_prefix='/products')

ALLOWED_EXTENSIONS = {'xlsx', 'xls'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@products_bp.route('/download-template')
@login_required
def download_template():
    """Download Excel template for bulk update."""
    from flask import send_from_directory
    import os
    
    # Path to public folder
    public_dir = os.path.join(current_app.root_path, '..', 'public')
    return send_from_directory(public_dir, 'template.xlsx', as_attachment=True)

@products_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create_product():
    """Manually create a new product."""
    if request.method == 'POST':
        try:
            import json
            import os
            from werkzeug.utils import secure_filename
            
            # Basic Fields
            barcode = request.form.get('barcode', '').strip()
            title = request.form.get('title', '').strip()
            price = float(request.form.get('price', 0) or 0)
            quantity = int(request.form.get('quantity', 0) or 0)
            stock_code = request.form.get('stock_code', '')
            cost = float(request.form.get('cost_price', 0) or 0)
            desi = float(request.form.get('desi', 1.0) or 1.0)
            
            brand = request.form.get('brand', '')
            category = request.form.get('category', '')
            description = request.form.get('description', '')
            
            # Attributes JSON
            attr_names = request.form.getlist('attr_name[]')
            attr_values = request.form.getlist('attr_value[]')
            attributes = {}
            for n, v in zip(attr_names, attr_values):
                if n.strip() and v.strip():
                    attributes[n.strip()] = v.strip()
            attributes_json = json.dumps(attributes) if attributes else None
            
            if not barcode or not title:
                flash('Barkod ve Ürün Adı zorunludur.', 'danger')
                return redirect(request.url)

            # Check existing
            existing = Product.query.filter_by(user_id=current_user.id, barcode=barcode).first()
            if existing:
                flash('Bu barkod zaten kullanımda.', 'warning')
                return redirect(request.url)
            
            # Handle Images
            images = request.files.getlist('images')
            image_urls = []
            
            # Ensure upload dir exists (using static folder for serving)
            upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
            os.makedirs(upload_folder, exist_ok=True)
            
            for img in images:
                if img and img.filename:
                    fname = secure_filename(f"{barcode}_{img.filename}")
                    if len(image_urls) >= 7: break
                    
                    fpath = os.path.join(upload_folder, fname)
                    img.save(fpath)
                    # Store relative path for URL
                    image_urls.append(url_for('static', filename=f'uploads/products/{fname}'))
            
            images_json = json.dumps(image_urls) if image_urls else None

            # Create Product
            new_prod = Product(
                user_id=current_user.id,
                barcode=barcode,
                title=title,
                listPrice=price,
                quantity=quantity,
                stockCode=stock_code,
                cost_price=cost,
                brand=brand,
                top_category=category,
                description=description,
                desi=desi,
                vatRate=float(request.form.get('vat_rate', 20.0)),
                attributes_json=attributes_json,
                images_json=images_json
            )
            
            db.session.add(new_prod)
            db.session.commit()
            flash(f'"{title}" başarıyla oluşturuldu.', 'success')
            return redirect(url_for('products.create_product'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Hata: {str(e)}', 'danger')
                
    return render_template('products/create_product.html')

@products_bp.route('/bulk-update', methods=['GET', 'POST'])
@login_required
def bulk_update():
    """Bulk update product stock and prices via Excel."""
    if request.method == 'POST':
        # Check plan permission (BUG-Z Restriction)
        if not current_user.has_plan_feature('add_excel_source'):
             flash('Bu özellik paketinizde kısıtlıdır.', 'danger')
             return redirect(request.url)
             
        if 'file' not in request.files:
            flash('Dosya seçilmedi.', 'danger')
            return redirect(request.url)
            
        file = request.files['file']
        target = request.form.get('target', 'local')
        
        if file.filename == '':
            flash('Dosya seçilmedi.', 'danger')
            return redirect(request.url)
            
        if file and allowed_file(file.filename):
            try:
                # Read Excel into list of dicts first
                df = pd.read_excel(file)
                
                cols = {c.lower(): c for c in df.columns}
                barcode_col = next((c for c in cols if 'barkod' in c or 'barcode' in c), None)
                stock_col = next((c for c in cols if 'stok' in c or 'stock' in c or 'adet' in c), None)
                price_col = next((c for c in cols if 'fiyat' in c or 'price' in c or 'tutar' in c), None)
                
                if not barcode_col:
                    flash('Excel dosyasında "Barkod" sütunu bulunamadı.', 'danger')
                    return redirect(request.url)
                    
                if not stock_col and not price_col:
                    flash('Excel dosyasında "Stok" veya "Fiyat" sütunu bulunamadı.', 'danger')
                    return redirect(request.url)

                # Convert to standard format list
                items = []
                for index, row in df.iterrows():
                    barcode = str(row[cols[barcode_col]]).strip()
                    if not barcode or barcode.lower() == 'nan':
                        continue
                        
                    item = {'barcode': barcode}
                    if stock_col:
                        try:
                            item['stock'] = int(row[cols[stock_col]])
                        except:
                            pass
                    if price_col:
                        try:
                            item['price'] = float(row[cols[price_col]])
                        except:
                            pass
                    
                    if 'stock' in item or 'price' in item:
                        items.append(item)

                if not items:
                    flash('Excel dosyasında güncellenecek geçerli satır bulunamadı.', 'warning')
                    return redirect(request.url)

                # Dispatch based on target
                if target == 'local':
                    # EXISTING LOCAL LOGIC
                    updated_count = 0
                    not_found_count = 0
                    
                    for item in items:
                        product = Product.query.filter_by(user_id=current_user.id, barcode=item['barcode']).first()
                        if product:
                            changes = False
                            if 'stock' in item and product.stock != item['stock']:
                                product.stock = item['stock']
                                changes = True
                                
                            if 'price' in item and product.price != item['price']:
                                product.price = item['price']
                                changes = True
                                
                            if changes:
                                updated_count += 1
                        else:
                            not_found_count += 1
                    
                    if updated_count > 0:
                        db.session.commit()
                        AdminLog.log_action(current_user.id, 'bulk_update_excel', details=f'Local update: {updated_count} items')
                        flash(f'{updated_count} ürün yerel veritabanında güncellendi. {not_found_count} bulunamadı.', 'success')
                    else:
                        flash(f'Hiçbir ürün güncellenmedi. {not_found_count} barkod bulunamadı.', 'warning')

                elif target == 'trendyol':
                    from app.services.job_queue import submit_mp_job, is_job_running_for_user
                    from app.services.trendyol_service import perform_trendyol_batch_update
                    
                    # Concurrency Check: Prevent new sync if one is already running
                    if is_job_running_for_user(current_user.id):
                        flash('Zaten devam eden bir işlem var. Lütfen tamamlanmasını bekleyin.', 'warning')
                        return redirect(url_for('products.bulk_update'))
                    
                    job_id = submit_mp_job(
                        'trendyol_excel_update', 'trendyol',
                        lambda jid: perform_trendyol_batch_update(jid, items),
                        params={'count': len(items)}
                    )
                    flash(f'Trendyol güncelleme işlemi başlatıldı (Job ID: {job_id}). {len(items)} satır işleniyor.', 'success')
                    # Optionally redirect to logs?
                    
                elif target == 'n11':
                    from app.services.job_queue import submit_mp_job, is_job_running_for_user
                    from app.services.n11_service import perform_n11_batch_update
                    
                    # Concurrency Check
                    if is_job_running_for_user(current_user.id):
                        flash('Zaten devam eden bir işlem var. Lütfen tamamlanmasını bekleyin.', 'warning')
                        return redirect(url_for('products.bulk_update'))
                    
                    job_id = submit_mp_job(
                        'n11_excel_update', 'n11',
                        lambda jid: perform_n11_batch_update(jid, items),
                        params={'count': len(items)}
                    )
                    flash(f'N11 güncelleme işlemi başlatıldı (Job ID: {job_id}).', 'success')

                elif target == 'pazarama':
                    from app.services.job_queue import submit_mp_job, is_job_running_for_user
                    from app.services.pazarama_service import perform_pazarama_batch_update
                    
                    # Concurrency Check
                    if is_job_running_for_user(current_user.id):
                        flash('Zaten devam eden bir işlem var. Lütfen tamamlanmasını bekleyin.', 'warning')
                        return redirect(url_for('products.bulk_update'))
                    
                    job_id = submit_mp_job(
                        'pazarama_excel_update', 'pazarama',
                        lambda jid: perform_pazarama_batch_update(jid, items),
                        params={'count': len(items)}
                    )
                    flash(f'Pazarama güncelleme işlemi başlatıldı (Job ID: {job_id}).', 'success')
                
                else:
                    flash('Geçersiz hedef seçimi.', 'danger')

            except Exception as e:
                flash(f'Hata oluştu: {str(e)}', 'danger')
                
            return redirect(url_for('products.bulk_update'))
        
        else:
            flash('Geçersiz dosya formatı.', 'danger')
            return redirect(request.url)

    return render_template('products/bulk_update.html')

# ---------------- Blacklist Management ----------------

@products_bp.route('/blacklist', methods=['GET', 'POST'])
@login_required
def blacklist():
    """Manage banned brands and categories."""
    from app.models import Blacklist
    
    if request.method == 'POST':
        b_type = request.form.get('type')
        value = request.form.get('value')
        reason = request.form.get('reason')
        
        if b_type and value:
            # Check if exists
            exists = Blacklist.query.filter_by(user_id=current_user.id, type=b_type, value=value).first()
            if exists:
                flash(f'"{value}" zaten yasaklı listesinde.', 'warning')
            else:
                item = Blacklist(user_id=current_user.id, type=b_type, value=value, reason=reason)
                db.session.add(item)
                db.session.commit()
                flash(f'"{value}" yasaklı listesine eklendi.', 'success')
        else:
            flash('Tür ve Değer alanları zorunludur.', 'danger')
            
        return redirect(url_for('products.blacklist'))
        
    # List items
    blacklist_items = Blacklist.query.filter_by(user_id=current_user.id).order_by(Blacklist.created_at.desc()).all()
    return render_template('products/blacklist.html', items=blacklist_items)

@products_bp.route('/blacklist/<int:id>/delete', methods=['POST'])
@login_required
def delete_blacklist(id):
    """Remove item from blacklist."""
    from app.models import Blacklist
    
    item = Blacklist.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    value = item.value
    db.session.delete(item)
    db.session.commit()
    
    flash(f'"{value}" yasaklı listesinden kaldırıldı.', 'success')
    return redirect(url_for('products.blacklist'))

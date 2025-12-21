"""
APScheduler tabanlı otomatik görev zamanlayıcı
Pazaryeri senkronizasyonlarını periyodik olarak çalıştırır
"""
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask
from app import db
from app.models import AutoSync
from app.services.auto_sync_service import sync_marketplace_products
from app.services.image_template_service import ImageTemplateService
from app.services.instagram_service import publish_photo


logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: BackgroundScheduler = None


def init_scheduler(app: Flask):
    """
    Flask app ile scheduler'ı başlat ve kayıtlı job'ları yükle
    """
    global scheduler
    
    if scheduler is not None:
        logger.warning("Scheduler already initialized")
        return scheduler
    
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()
    logger.info("Scheduler started")
    
    # Uygulama kapatıldığında scheduler'ı durdur
    import atexit
    atexit.register(lambda: scheduler.shutdown() if scheduler else None)
    
    # Flask app context içinde çalışması için wrapper
    def sync_job_wrapper(marketplace: str, user_id: int):
        with app.app_context():
            try:
                sync_marketplace_task(marketplace, user_id=user_id)
            except Exception as e:
                logger.exception(f"Error in sync job for {marketplace} (User: {user_id}): {e}")
    
    # Kayıtlı senkronizasyon ayarlarını yükle
    with app.app_context():
        _load_sync_jobs(sync_job_wrapper)
    
    return scheduler


def _load_sync_jobs(job_wrapper_func):
    """Veritabanından aktif senkronizasyon ayarlarını yükle ve job'ları ekle"""
    from app.models import Setting, AutoSync

    try:
        # 1. Marketplace Product Sync Jobs
        active_syncs = AutoSync.query.filter_by(enabled=True).all()
        
        for sync in active_syncs:
            if not sync.user_id:
                continue
                
            job_id = f"sync_{sync.marketplace}_{sync.user_id}"
            
            # Mevcut job'u kontrol et
            if scheduler.get_job(job_id):
                continue
            
            # Yeni job ekle
            scheduler.add_job(
                func=job_wrapper_func,
                args=[sync.marketplace, sync.user_id],
                trigger=IntervalTrigger(minutes=sync.sync_interval_minutes),
                id=job_id,
                name=f"Auto Sync {sync.marketplace.capitalize()} (User: {sync.user_id})",
                replace_existing=True
            )
            logger.info(f"Loaded sync job for {sync.marketplace} (User: {sync.user_id}, interval: {sync.sync_interval_minutes} min)")

        # 2. Global Order Sync Job
        # We need a wrapper for order sync too because it needs app context
        # But _load_sync_jobs is called inside an app context wrapper in init_scheduler
        # Wait, the job function itself needs app context when RUNNING.
        # The wrapper passed here 'job_wrapper_func' takes 'marketplace' arg.
        # We need a different wrapper or use a lambda?
        # Let's create a dedicated wrapper for order sync in init_scheduler or add_order_sync_job
        
        order_sync_enabled = Setting.get('ORDER_SYNC_ENABLED') == 'true'
        if order_sync_enabled:
            interval = int(Setting.get('ORDER_SYNC_INTERVAL') or 60)
            add_order_sync_job(interval)

    except Exception as e:
        logger.exception(f"Error loading sync jobs: {e}")


def add_order_sync_job(user_id: int, interval_minutes: int = 60):
    """
    Kullanıcı için pazaryerlerinden sipariş çekme job'u ekle
    """
    if scheduler is None:
        logger.error("Scheduler not initialized")
        return False
        
    job_id = f"order_sync_{user_id}"
    
    try:
        from flask import current_app
        from app.services.order_service import sync_all_orders
        
        # Wrapper to provide context
        def order_sync_wrapper():
            with current_app.app_context():
                try:
                    logger.info(f"Running Order Sync Task for User: {user_id}...")
                    sync_all_orders(user_id=user_id)
                    logger.info(f"Order Sync Task for User: {user_id} Completed")
                except Exception as e:
                    logger.error(f"Order Sync for User: {user_id} Failed: {e}")

        scheduler.add_job(
            func=order_sync_wrapper,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=job_id,
            name=f"Order Sync (User: {user_id})",
            replace_existing=True
        )
        logger.info(f"Added Order Sync job for User {user_id} (interval: {interval_minutes} min)")
        return True
    
    except Exception as e:
        logger.exception(f"Error adding order sync job for User {user_id}: {e}")
        return False


def remove_order_sync_job(user_id: int):
    """Kullanıcı için sipariş çekme job'unu kaldır"""
    if scheduler is None:
        return False
        
    job_id = f"order_sync_{user_id}"
    
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"Removed Order Sync job for User {user_id}")
            return True
        return True # Already removed
    except Exception as e:
        logger.exception(f"Error removing order sync job for User {user_id}: {e}")
        return False


def add_sync_job(marketplace: str, user_id: int, interval_minutes: int = 60):
    """
    Pazaryeri için senkronizasyon job'u ekle
    
    Args:
        marketplace: Pazaryeri adı
        user_id: Kullanıcı ID
        interval_minutes: Senkronizasyon aralığı (dakika)
    """
    if scheduler is None:
        logger.error("Scheduler not initialized")
        return False
    
    job_id = f"sync_{marketplace}_{user_id}"
    
    try:
        # Flask app context gerekli
        from flask import current_app
        
        def sync_job_wrapper():
            with current_app.app_context():
                sync_marketplace_task(marketplace, user_id=user_id)
        
        # Job'u ekle veya güncelle
        scheduler.add_job(
            func=sync_job_wrapper,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=job_id,
            name=f"Auto Sync {marketplace.capitalize()} (User: {user_id})",
            replace_existing=True
        )
        
        logger.info(f"Added/updated sync job for {marketplace} (User: {user_id}, interval: {interval_minutes} min)")
        return True
    
    except Exception as e:
        logger.exception(f"Error adding sync job for {marketplace} (User: {user_id}): {e}")
        return False


def remove_sync_job(marketplace: str, user_id: int):
    """
    Pazaryeri için senkronizasyon job'unu kaldır
    
    Args:
        marketplace: Pazaryeri adı
        user_id: Kullanıcı ID
    """
    if scheduler is None:
        logger.error("Scheduler not initialized")
        return False
    
    job_id = f"sync_{marketplace}_{user_id}"
    
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"Removed sync job for {marketplace} (User: {user_id})")
            return True
        else:
            logger.warning(f"No job found for {marketplace} (User: {user_id})")
            return True  # No job exists = already disabled = success
            
    except Exception as e:
        logger.exception(f"Error removing sync job for {marketplace} (User: {user_id}): {e}")
        return False


def sync_marketplace_task(marketplace: str, user_id: int):
    """
    Pazaryeri senkronizasyon görevi
    Scheduler tarafından periyodik olarak çağrılır
    
    Args:
        marketplace: Pazaryeri adı
        user_id: Kullanıcı ID
    """
    logger.info(f"Running scheduled sync for {marketplace} (User: {user_id})")
    
    try:
        result = sync_marketplace_products(marketplace, user_id=user_id)
        
        if result.get('success'):
            logger.info(f"Sync successful for {marketplace} (User: {user_id}): "
                       f"{result.get('products_updated', 0)} products updated, "
                       f"{result.get('stock_changes', 0)} stock changes, "
                       f"{result.get('price_changes', 0)} price changes")
        else:
            logger.warning(f"Sync completed with errors for {marketplace} (User: {user_id}): {result.get('errors', [])}")
            
    except Exception as e:
        logger.exception(f"Sync task failed for {marketplace} (User: {user_id}): {e}")



def execute_instagram_task(job_data: dict):
    """
    Scheduled task to publish an Instagram story or post
    """
    title = job_data.get('title')
    media_type = job_data.get('media_type', 'story') # story or post
    logger.info(f"Executing Instagram ({media_type}) task: {title}")
    
    try:
        # 1. Generate Image
        image_url = job_data.get('image_url')
        if not image_url:
            logger.error("No image URL provided for task")
            return
            
        generated_image_path = ImageTemplateService.create_story_image(
            image_url=image_url,
            title=title,
            price=job_data.get('price'),
            discount_price=job_data.get('discount_price'),
            template_style=job_data.get('template_style', 'modern')
        )
        
        if not generated_image_path:
            logger.error("Failed to generate image")
            return
            
        logger.info(f"Image generated at: {generated_image_path}")
        
        # 2. Publish
        # In a real scenario, we would upload the generated image to a public URL or use the API.
        # Since we are local, we simulate the 'Publish' step or try to publish if configured.
        
        caption = job_data.get('caption', f"{title} - {job_data.get('price')} TL")
        
        # For now, we log the success.
        logger.info(f"Instagram {media_type.upper()} published (Simulation). Caption: {caption}")
        
    except Exception as e:
        logger.exception(f"Instagram task failed: {e}")


def add_instagram_job(job_data: dict, run_date: datetime):
    """
    Add a one-time job for Instagram Story or Post
    """
    if scheduler is None:
        return False
        
    media_type = job_data.get('media_type', 'story')
    job_id = f"ig_{media_type}_{int(datetime.now().timestamp())}"
    
    try:
        scheduler.add_job(
            func=execute_instagram_task,
            trigger='date',
            run_date=run_date,
            args=[job_data],
            id=job_id,
            name=f"{media_type.capitalize()}: {job_data.get('title')}"
        )
        logger.info(f"Scheduled Instagram {media_type} job {job_id} for {run_date}")
        return True
    except Exception as e:
        logger.error(f"Failed to schedule Instagram job: {e}")
        return False

def get_scheduled_instagram_jobs():
    """Returns list of pending Instagram jobs"""
    if scheduler is None:
        return []
        
    jobs = []
    for job in scheduler.get_jobs():
        if job.id.startswith('ig_'):
            jobs.append({
                'id': job.id,
                'name': job.name,
                'run_time': job.next_run_time,
                'args': job.args[0] if job.args else {}
            })
    return sorted(jobs, key=lambda x: x['run_time'])


def get_scheduler_status() -> dict:
    """Scheduler durumunu döndür"""
    if scheduler is None:
        return {
            'running': False,
            'jobs': []
        }
    
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'name': job.name,
            'next_run': job.next_run_time.isoformat() if job.next_run_time else None
        })
    
    return {
        'running': scheduler.running,
        'jobs': jobs
    }

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
from app.services.auto_sync_service import sync_all_users_marketplace
from app.services.image_template_service import ImageTemplateService
from app.services.instagram_service import publish_photo


logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: BackgroundScheduler = None
_flask_app: Flask = None


def init_scheduler(app: Flask):
    """
    Flask app ile scheduler'ı başlat ve kayıtlı job'ları yükle
    """
    global scheduler, _flask_app
    _flask_app = app
    
    import os
    lock_file = "scheduler.lock"
    try:
        # Simple file lock for Windows/Linux to prevent multiple schedulers
        if os.path.exists(lock_file):
            try: os.remove(lock_file)
            except: pass
        
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        # If we reached here, we own the lock
    except Exception:
        logger.info("Scheduler already running in another process, skipping init.")
        return None

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()
    logger.info("Scheduler started successfully (Primary Instance)")
    
    # Uygulama kapatıldığında scheduler'ı durdur ve kilidi kaldır
    import atexit
    def shutdown():
        if scheduler: scheduler.shutdown()
        try: os.close(fd); os.remove(lock_file)
        except: pass
    atexit.register(shutdown)
    
    # Flask app context içinde çalışması için wrapper
    def sync_job_wrapper(marketplace: str):
        with app.app_context():
            try:
                sync_marketplace_task(marketplace)
            except Exception as e:
                logger.exception(f"Error in sync job for {marketplace}: {e}")
    
    # Kayıtlı senkronizasyon ayarlarını yükle
    with app.app_context():
        _load_sync_jobs(sync_job_wrapper)
    
    return scheduler


def _load_sync_jobs(job_wrapper_func):
    """Sistem çapında senkronizasyon job'larını yükle (Her 1 saatte bir)"""
    from app.models import Setting
    
    marketplaces = ['trendyol', 'n11', 'pazarama', 'hepsiburada', 'idefix']
    
    try:
        # 1. Her pazaryeri için saatlik toplu senkronizasyon job'u ekle
        for mp in marketplaces:
            job_id = f"sync_{mp}_global"
            
            scheduler.add_job(
                func=job_wrapper_func,
                args=[mp],
                trigger=IntervalTrigger(minutes=480), # Her zaman 480 dk (8 Saat)
                id=job_id,
                name=f"Global Hourly Sync {mp.capitalize()}",
                replace_existing=True
            )
            logger.info(f"Loaded global hourly sync job for {mp}")

        # 2. Global Order Sync Job
        # Sipariş çekme her zaman aktif ve 60 dk olsun (veya ayarlardan alabiliriz ama 60 dk varsayılan)
        order_sync_enabled = Setting.get('ORDER_SYNC_ENABLED') != 'false' # Default true if not explicitly false
        if order_sync_enabled:
            interval = int(Setting.get('ORDER_SYNC_INTERVAL') or 60)
            add_order_sync_job(interval)

    except Exception as e:
        logger.exception(f"Error loading sync jobs: {e}")


def add_order_sync_job(interval_minutes: int = 60):
    """
    Tüm pazaryerlerinden sipariş çekme job'u ekle
    """
    if scheduler is None:
        logger.error("Scheduler not initialized")
        return False
        
    job_id = "global_order_sync"
    
    try:
        from flask import current_app
        from app.services.order_service import sync_all_users_orders
        
        # Wrapper to provide context
        def order_sync_wrapper():
            if _flask_app:
                with _flask_app.app_context():
                    try:
                        logger.info("Running Global Order Sync Task...")
                        sync_all_users_orders()
                        logger.info("Global Order Sync Task Completed")
                    except Exception as e:
                        logger.error(f"Global Order Sync Failed: {e}")
            else:
                logger.error("Flask app instance not found for order sync")

        scheduler.add_job(
            func=order_sync_wrapper,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=job_id,
            name="Global Order Sync",
            replace_existing=True
        )
        logger.info(f"Added Global Order Sync job (interval: {interval_minutes} min)")
        return True
    
    except Exception as e:
        logger.exception(f"Error adding order sync job: {e}")
        return False



def remove_order_sync_job():
    """Tüm pazaryerlerinden sipariş çekme job'unu kaldır"""
    if scheduler is None:
        return False
        
    job_id = "global_order_sync"
    
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info("Removed Global Order Sync job")
            return True
        return True # Already removed
    except Exception as e:
        logger.exception(f"Error removing order sync job: {e}")
        return False


def add_sync_job(marketplace: str, interval_minutes: int = 480):
    """
    Pazaryeri için senkronizasyon job'u ekle
    
    Args:
        marketplace: Pazaryeri adı
        interval_minutes: Senkronizasyon aralığı (dakika)
    """
    if scheduler is None:
        logger.error("Scheduler not initialized")
        return False
    
    job_id = f"sync_{marketplace}"
    
    try:
        # Flask app context gerekli
        from flask import current_app
        
        def sync_job_wrapper():
            if _flask_app:
                with _flask_app.app_context():
                    sync_marketplace_task(marketplace)
            else:
                logger.error(f"Flask app instance not found for {marketplace} sync")
        
        # Job'u ekle veya güncelle
        scheduler.add_job(
            func=sync_job_wrapper,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=job_id,
            name=f"Auto Sync {marketplace.capitalize()}",
            replace_existing=True
        )
        
        logger.info(f"Added/updated sync job for {marketplace} (interval: {interval_minutes} min)")
        return True
        
    except Exception as e:
        logger.exception(f"Error adding sync job for {marketplace}: {e}")
        return False


def remove_sync_job(marketplace: str):
    """
    Pazaryeri için senkronizasyon job'unu kaldır
    
    Args:
        marketplace: Pazaryeri adı
    """
    if scheduler is None:
        logger.error("Scheduler not initialized")
        return False
    
    job_id = f"sync_{marketplace}"
    
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"Removed sync job for {marketplace}")
            return True
        else:
            logger.warning(f"No job found for {marketplace}")
            return True  # No job exists = already disabled = success
            
    except Exception as e:
        logger.exception(f"Error removing sync job for {marketplace}: {e}")
        return False


def sync_marketplace_task(marketplace: str):
    """
    Pazaryeri senkronizasyon görevi
    Scheduler tarafından periyodik olarak çağrılır
    
    Args:
        marketplace: Pazaryeri adı
    """
    logger.info(f"Running scheduled sync for {marketplace}")
    
    try:
        result = sync_all_users_marketplace(marketplace)
        
        if result.get('total', 0) > 0:
            logger.info(f"Global sync finished for {marketplace}: "
                       f"{result.get('success', 0)} of {result.get('total', 0)} sessions successful")
        else:
            logger.info(f"No active auto-sync sessions for {marketplace}")
            
    except Exception as e:
        logger.exception(f"Global sync task failed for {marketplace}: {e}")
    finally:
        # Reschedule next run (8 hours after completion)
        from app.models import Setting
        enabled = Setting.get(f"SYNC_ENABLED_{marketplace}") == "true"
        if enabled:
            logger.info(f"Rescheduling next sync for {marketplace} in 8 hours.")
            add_sync_job(marketplace, 480)



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

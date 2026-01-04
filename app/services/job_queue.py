import uuid
import logging
import threading
import concurrent.futures
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from flask import current_app
from config import Config
from app import db
from app.models import BatchLog, PersistentJob

from flask_login import current_user

# Max memory workers for actual execution
MP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=30)


def is_job_running_for_user(user_id: int, job_type: str = None) -> bool:
    """
    Check if there's an active job running for the given user in DB.
    """
    try:
        query = PersistentJob.query.filter(
            PersistentJob.user_id == user_id,
            PersistentJob.status.in_(['pending', 'running', 'pausing'])
        )
        if job_type:
            query = query.filter(PersistentJob.job_type == job_type)
        return query.first() is not None
    except Exception as e:
        logging.error(f"Error checking running job: {e}")
        return False


def get_running_job_for_user(user_id: int, job_type: str = None) -> Optional[Dict[str, Any]]:
    """
    Get the currently running job for a user from DB.
    """
    try:
        query = PersistentJob.query.filter(
            PersistentJob.user_id == user_id,
            PersistentJob.status.in_(['pending', 'running', 'pausing'])
        )
        if job_type:
            query = query.filter(PersistentJob.job_type == job_type)
        
        job = query.order_by(PersistentJob.created_at.desc()).first()
        return serialize_job(job) if job else None
    except Exception as e:
        logging.error(f"Error getting running job: {e}")
        return None

def _sync_with_batch_log(job: PersistentJob):
    """Bridge between modern PersistentJob and legacy BatchLog if needed."""
    try:
        log = BatchLog.query.filter_by(batch_id=job.id).first()
        if not log:
            log = BatchLog(
                batch_id=job.id,
                timestamp=job.created_at.isoformat() if job.created_at else datetime.now().isoformat(),
                success=False,
                marketplace=job.marketplace or 'unknown',
                job_type=job.job_type,
                user_id=job.user_id,
                product_count=job.progress_total,
                details_json=json.dumps(serialize_job(job))
            )
            db.session.add(log)
        else:
            log.details_json = json.dumps(serialize_job(job))
            log.job_type = job.job_type
            log.marketplace = job.marketplace
            
            if job.status == 'completed':
                res = json.loads(job.result_json) if job.result_json else {}
                if isinstance(res, dict):
                    summary = res.get('summary', {})
                    s_count = res.get('success_count', summary.get('success_count', 0))
                    f_count = res.get('fail_count', summary.get('fail_count', 0))
                    log.product_count = res.get('count', s_count + f_count)
                    log.success_count = s_count
                    log.fail_count = f_count
                    log.success = (f_count == 0) and res.get('success', True)
                else:
                    log.success = True
            elif job.status == 'failed':
                log.success = False
        
        db.session.commit()
    except Exception as e:
        logging.error(f"Failed to sync batch log: {e}")
        db.session.rollback()

def register_mp_job(job_type: str, marketplace: str, params: Optional[Dict[str, Any]] = None) -> str:
    job_id = str(uuid.uuid4())
    user_id = params.get('_user_id') if params else None
    
    job = PersistentJob(
        id=job_id,
        user_id=user_id,
        marketplace=marketplace,
        job_type=job_type,
        status='pending',
        params_json=json.dumps(params or {}),
        progress_total=100,
        logs_json=json.dumps([])
    )
    
    db.session.add(job)
    db.session.commit()
    
    logging.info("Job created in DB: %s (%s.%s)", job_id, marketplace, job_type)
    _sync_with_batch_log(job)
    
    return job_id

def serialize_job(job: PersistentJob) -> Dict[str, Any]:
    if not job: return {}
    return {
        'id': job.id,
        'user_id': job.user_id,
        'marketplace': job.marketplace,
        'job_type': job.job_type,
        'status': job.status,
        'progress_current': job.progress_current,
        'progress_total': job.progress_total,
        'progress_message': job.progress_message,
        'params': job.get_params(),
        'result': json.loads(job.result_json) if job.result_json else None,
        'logs': job.get_logs(),
        'cancel_requested': job.cancel_requested,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None
    }

def get_mp_job(job_id: str) -> Optional[Dict[str, Any]]:
    # Force expire session to get fresh data from other workers
    db.session.expire_all()
    job = PersistentJob.query.get(job_id)
    return serialize_job(job) if job else None

def get_all_jobs() -> list:
    jobs = PersistentJob.query.order_by(PersistentJob.created_at.desc()).limit(100).all()
    return [serialize_job(job) for job in jobs]

def clear_all_jobs() -> int:
    # We don't delete from DB, but we could mark old ones as failed or something
    # For now, let's keep DB persistence.
    return 0


def control_mp_job(job_id: str, action: str) -> bool:
    job = PersistentJob.query.get(job_id)
    if not job:
        return False
    
    if action == 'cancel':
        job.cancel_requested = True
        job.status = 'cancelling'
        logging.info(f"Job {job_id} cancel requested in DB")
    elif action == 'pause':
        # job.pause_requested = True # Not in model yet, use params
        job.status = 'pausing'
    elif action == 'resume':
        job.status = 'running' 
    else:
        return False
        
    db.session.commit()
    _sync_with_batch_log(job)
    return True

def update_mp_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    # Force expire to get updates from actual running thread
    db.session.expire_all()
    job = PersistentJob.query.get(job_id)
    if not job:
        return None
    
    for key, value in fields.items():
        if hasattr(job, key):
            setattr(job, key, value)
        elif key == 'result':
            job.result_json = json.dumps(value)
        elif key == 'error':
            # Store error in result or specific field if we add it
            pass
            
    db.session.commit()
    _sync_with_batch_log(job)
    return serialize_job(job)

def append_mp_job_log(job_id: str, message: str, level: str = 'info') -> None:
    append_mp_job_logs(job_id, [message], level=level)

def append_mp_job_logs(job_id: str, messages: List[str], level: str = 'info') -> None:
    if not messages:
        return

    level_normalized = level.upper()
    ts = datetime.utcnow().isoformat()
    
    new_entries = []
    for msg in messages:
        new_entries.append({
            'ts': ts,
            'level': level_normalized,
            'message': msg,
        })
    
    job = PersistentJob.query.get(job_id)
    if not job:
        return
        
    logs = job.get_logs()
    logs.extend(new_entries)
    job.logs_json = json.dumps(logs)
    db.session.commit()
    
    # Still persist to BatchLog details
    _sync_with_batch_log(job)

    # Log to system console (just the last one or summary if too many)
    params = job.get_params()
    u_email = params.get('_user_email')
    u_id = job.user_id
    
    user_tag = ""
    if u_email:
        user_tag = f" (User: {u_email})"
    elif u_id:
        user_tag = f" (User ID: {u_id})"
        
    if len(messages) == 1:
        logging.log(getattr(logging, level_normalized, logging.INFO), "[%s]%s %s", job_id, user_tag, messages[0])
    else:
        logging.log(getattr(logging, level_normalized, logging.INFO), "[%s]%s Added %d logs (Last: %s)", job_id, user_tag, len(messages), messages[-1])

def update_job_progress(job_id: str, current: int, total: int = None, message: str = None):
    """Accurate progress update helper."""
    job = PersistentJob.query.get(job_id)
    if job:
        job.progress_current = current
        if total is not None:
            job.progress_total = total
        if message:
            job.progress_message = message
        db.session.commit()
        _sync_with_batch_log(job)

def submit_mp_job(job_type: str, marketplace: str, func, params: Optional[Dict[str, Any]] = None) -> str:
    # Capture user_id if authenticated
    try:
        if current_user and current_user.is_authenticated:
            if params is None:
                params = {}
            params['_user_id'] = current_user.id
            params['_user_email'] = current_user.email
    except Exception:
        pass # Ignore auth errors in submission if any

    job_id = register_mp_job(job_type, marketplace, params=params)
    
    app = current_app._get_current_object()

    def _runner():
        with app.app_context():
            # Wait/Queue Management: Limit to max 3 concurrent running jobs
            import random
            while True:
                db.session.expire_all()
                job = PersistentJob.query.get(job_id)
                if not job or job.cancel_requested:
                    if job:
                        job.status = 'cancelled'
                        db.session.commit()
                    return

                # Check current running jobs (Global across all workers via DB)
                running_count = PersistentJob.query.filter_by(status='running').count()
                if running_count < 10:
                    break # Slot available!
                
                # Wait for a slot
                time.sleep(10 + random.random() * 5)

            job.status = 'running'
            job.started_at = datetime.now()
            db.session.commit()
            
            append_mp_job_log(job_id, "Başladı", level='info')
            
            try:
                result = func(job_id)
                
                # Check for cancellation
                db.session.refresh(job)
                if job.cancel_requested:
                    append_mp_job_log(job_id, "İptal edildi", level='warning')
                    job.status = 'cancelled'
                else:
                    append_mp_job_log(job_id, "Tamamlandı", level='info')
                    job.status = 'completed'
                
                job.result_json = json.dumps(result)
                job.completed_at = datetime.now()
                job.progress_current = job.progress_total
                db.session.commit()
                _sync_with_batch_log(job)
                
            except Exception as exc:
                logging.exception("Job failed: %s", job_id)
                db.session.rollback()
                
                # Reload job to save error state
                job = PersistentJob.query.get(job_id)
                job.status = 'failed'
                job.completed_at = datetime.now()
                db.session.commit()
                
                append_mp_job_log(job_id, f"Hata: {exc}", level='error')
                _sync_with_batch_log(job)

    MP_EXECUTOR.submit(_runner)
    return job_id

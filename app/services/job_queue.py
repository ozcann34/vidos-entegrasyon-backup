import uuid
import logging
import threading
import concurrent.futures
import json
from datetime import datetime
from typing import Dict, Any, Optional
from flask import current_app
from config import Config
from app import db
from app.models import BatchLog

from flask_login import current_user

_MP_JOBS: Dict[str, Dict[str, Any]] = {}
_MP_JOBS_LOCK = threading.Lock()
_MP_MAX_JOBS = Config.MP_MAX_JOBS
MP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def is_job_running_for_user(user_id: int, job_type: str = None) -> bool:
    """
    Check if there's an active job running for the given user.
    Optionally filter by job_type (e.g., 'batch_send', 'sync').
    Returns True if a running/queued job exists.
    """
    with _MP_JOBS_LOCK:
        for job in _MP_JOBS.values():
            job_user_id = job.get('params', {}).get('_user_id')
            if job_user_id == user_id:
                status = job.get('status', '')
                if status in ('queued', 'running', 'pausing'):
                    # If job_type filter is specified, also check job type
                    if job_type:
                        if job.get('job_type') == job_type:
                            return True
                    else:
                        return True
    return False


def get_running_job_for_user(user_id: int, job_type: str = None) -> Optional[Dict[str, Any]]:
    """
    Get the currently running job for a user, if any.
    """
    with _MP_JOBS_LOCK:
        for job in _MP_JOBS.values():
            job_user_id = job.get('params', {}).get('_user_id')
            if job_user_id == user_id:
                status = job.get('status', '')
                if status in ('queued', 'running', 'pausing'):
                    if job_type:
                        if job.get('job_type') == job_type:
                            return serialize_job(job)
                    else:
                        return serialize_job(job)
    return None

def _persist_job(job_id: str, job_data: Dict[str, Any]):
    """Helper to save/update BatchLog in DB."""
    try:
        # We need app context. If running in thread, current_app might not work unless pushed.
        # But this function is called from functions that should have context.
        # Check if we have an active context
        if not current_app:
            return

        log = BatchLog.query.filter_by(batch_id=job_id).first()
        user_id = job_data.get('params', {}).get('_user_id')
        
        if not log:
            log = BatchLog(
                batch_id=job_id,
                timestamp=job_data.get('created_at'),
                success=False, # Default
                marketplace=job_data.get('marketplace', 'unknown'),
                job_type=job_data.get('job_type'),
                user_id=user_id,
                product_count=0,
                success_count=0,
                fail_count=0,
                details_json=json.dumps(job_data)
            )
            db.session.add(log)
        else:
            log.details_json = json.dumps(job_data)
            # Ensure marketplace is updated if it changed or was wrong
            if job_data.get('marketplace'):
                log.marketplace = job_data.get('marketplace')
            
            # Ensure job_type is set
            if job_data.get('job_type'):
                log.job_type = job_data.get('job_type')
            
            # Ensure user_id is set if missing
            if not log.user_id and user_id:
                log.user_id = user_id
            
            if job_data.get('status') == 'completed':
                # Check result for success/fail counts
                res = job_data.get('result') or {}
                if isinstance(res, dict):
                    # Check both top-level and nested summary for counts
                    summary = res.get('summary', {})
                    s_count = res.get('success_count', summary.get('success_count', 0))
                    f_count = res.get('fail_count', summary.get('fail_count', 0))
                    total_count = res.get('count', s_count + f_count)
                    
                    log.product_count = total_count
                    log.success_count = s_count
                    log.fail_count = f_count
                    log.success = (f_count == 0) and res.get('success', True)
                else:
                    log.success = True
            elif job_data.get('status') == 'failed':
                log.success = False
        
        db.session.commit()
    except Exception as e:
        logging.error(f"Failed to persist job {job_id}: {e}")
        db.session.rollback()

def register_mp_job(job_type: str, marketplace: str, params: Optional[Dict[str, Any]] = None) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.utcnow()
    record = {
        'id': job_id,
        'job_type': job_type,
        'marketplace': marketplace,
        'status': 'queued',
        'created_at': now.isoformat(),
        'updated_at': now.isoformat(),
        'created_ts': now.timestamp(),
        'params': params or {},
        'result': None,
        'error': None,
        'logs': [],
        'cancel_requested': False,
        'pause_requested': False
    }
    with _MP_JOBS_LOCK:
        if len(_MP_JOBS) >= _MP_MAX_JOBS:
            oldest_id = min(_MP_JOBS.items(), key=lambda item: item[1].get('created_ts', 0))[0]
            _MP_JOBS.pop(oldest_id, None)
        _MP_JOBS[job_id] = record
    
    logging.info("Job queued: %s (%s.%s)", job_id, marketplace, job_type)
    
    # Persist initial state
    _persist_job(job_id, record)
    
    return job_id

def serialize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    data = {k: (list(v) if k == 'logs' and isinstance(v, list) else v) for k, v in job.items() if k != 'created_ts'}
    return data

def get_mp_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _MP_JOBS_LOCK:
        job = _MP_JOBS.get(job_id)
        if not job:
            # Try DB if not in memory
            try:
                log = BatchLog.query.filter_by(batch_id=job_id).first()
                if log and log.details_json:
                    return json.loads(log.details_json)
            except Exception:
                pass
            return None
        return serialize_job(job)


def clear_all_jobs() -> int:
    """Clear all jobs from memory. Returns count of jobs cleared."""
    with _MP_JOBS_LOCK:
        count = len(_MP_JOBS)
        _MP_JOBS.clear()
        logging.info(f"Cleared {count} jobs from memory")
        return count


def control_mp_job(job_id: str, action: str) -> bool:
    """
    Control a running job.
    Actions: 'cancel', 'pause', 'resume'
    """
    with _MP_JOBS_LOCK:
        job = _MP_JOBS.get(job_id)
        if not job:
            return False
        
        if action == 'cancel':
            job['cancel_requested'] = True
            job['status'] = 'cancelling'
            logging.info(f"Job {job_id} cancel requested")
        elif action == 'pause':
            job['pause_requested'] = True
            job['status'] = 'pausing'
            logging.info(f"Job {job_id} pause requested")
        elif action == 'resume':
            job['pause_requested'] = False
            job['status'] = 'running' 
            logging.info(f"Job {job_id} resume requested")
        else:
            return False
            
        # Persist change
        _persist_job(job_id, job)
        return True

def update_mp_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _MP_JOBS_LOCK:
        job = _MP_JOBS.get(job_id)
        if not job:
            return None
        now_iso = datetime.utcnow().isoformat()
        job['updated_at'] = now_iso
        for key, value in fields.items():
            job[key] = value
        
        # Persist update
        _persist_job(job_id, job)
        
        return serialize_job(job)

def append_mp_job_log(job_id: str, message: str, level: str = 'info') -> None:
    level_normalized = level.upper()
    log_entry = {
        'ts': datetime.utcnow().isoformat(),
        'level': level_normalized,
        'message': message,
    }
    with _MP_JOBS_LOCK:
        job = _MP_JOBS.get(job_id)
        if not job:
            return
        job.setdefault('logs', []).append(log_entry)
        job['updated_at'] = log_entry['ts']
        
        _persist_job(job_id, job)

    logging.log(getattr(logging, level_normalized, logging.INFO), "[%s] %s", job_id, message)

def submit_mp_job(job_type: str, marketplace: str, func, params: Optional[Dict[str, Any]] = None) -> str:
    # Capture user_id if authenticated
    try:
        if current_user and current_user.is_authenticated:
            if params is None:
                params = {}
            params['_user_id'] = current_user.id
    except Exception:
        pass # Ignore auth errors in submission if any

    job_id = register_mp_job(job_type, marketplace, params=params)
    
    app = current_app._get_current_object()

    def _runner():
        with app.app_context():
            append_mp_job_log(job_id, "Başladı", level='info')
            update_mp_job(job_id, status='running')
            try:
                result = func(job_id)
                
                # Check for cancellation
                final_state = get_mp_job(job_id)
                if final_state and final_state.get('cancel_requested'):
                    append_mp_job_log(job_id, "İptal edildi", level='warning')
                    update_mp_job(job_id, status='cancelled', result=result, error="Kullanıcı tarafından iptal edildi")
                else:
                    append_mp_job_log(job_id, "Tamamlandı", level='info')
                    update_mp_job(job_id, status='completed', result=result, error=None)
            except Exception as exc:
                logging.exception("Job failed: %s", job_id)
                append_mp_job_log(job_id, f"Hata: {exc}", level='error')
                update_mp_job(job_id, status='failed', error=str(exc))

    MP_EXECUTOR.submit(_runner)
    return job_id

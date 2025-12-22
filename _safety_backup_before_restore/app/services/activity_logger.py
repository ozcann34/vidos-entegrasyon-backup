from flask import request
from app import db
from app.models.user_activity_log import UserActivityLog
import json

def log_user_activity(user_id, action, marketplace=None, details=None):
    """
    Log user activity for admin review.
    
    Args:
        user_id (int): ID of the user performing action.
        action (str): Short action code/name.
        marketplace (str, optional): Related marketplace.
        details (str or dict, optional): Extra info.
    """
    try:
        ip = request.remote_addr if request else None
        
        detail_str = ""
        if isinstance(details, (dict, list)):
            try:
                detail_str = json.dumps(details, ensure_ascii=False)
            except:
                detail_str = str(details)
        elif details:
            detail_str = str(details)
            
        log = UserActivityLog(
            user_id=user_id,
            action=action,
            marketplace=marketplace,
            details=detail_str,
            ip_address=ip
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        # Logging failure should not break the app
        print(f"Failed to log activity: {e}")

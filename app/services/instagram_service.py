import requests
from app.models.settings import Setting

def get_instagram_credentials(user_id=None):
    """
    Retrieve Instagram credentials from settings.
    Returns (account_id, access_token) or (None, None).
    """
    account_id = Setting.get("INSTAGRAM_ACCOUNT_ID", None, user_id=user_id)
    access_token = Setting.get("INSTAGRAM_ACCESS_TOKEN", None, user_id=user_id)
    return account_id, access_token

def create_media_container(image_url: str, caption: str, user_id=None):
    """
    Create a media container for an image.
    Step 1 of publishing.
    """
    account_id, access_token = get_instagram_credentials(user_id)
    
    if not account_id or not access_token:
        return {"success": False, "message": "Instagram kimlik bilgileri eksik. Lütfen ayarlardan giriniz."}

    url = f"https://graph.facebook.com/v18.0/{account_id}/media"
    payload = {
        "image_url": image_url,
        "caption": caption,
        "access_token": access_token
    }

    try:
        response = requests.post(url, data=payload)
        data = response.json()
        
        if response.status_code == 200 and "id" in data:
            return {"success": True, "container_id": data["id"]}
        else:
            error_msg = data.get("error", {}).get("message", "Bilinmeyen hata")
            return {"success": False, "message": f"Konteyner oluşturma hatası: {error_msg}"}
            
    except Exception as e:
        return {"success": False, "message": f"Bağlantı hatası: {str(e)}"}

def publish_media_container(creation_id: str, user_id=None):
    """
    Publish a media container.
    Step 2 of publishing.
    """
    account_id, access_token = get_instagram_credentials(user_id)
    
    if not account_id or not access_token:
        return {"success": False, "message": "Instagram kimlik bilgileri eksik."}

    url = f"https://graph.facebook.com/v18.0/{account_id}/media_publish"
    payload = {
        "creation_id": creation_id,
        "access_token": access_token
    }

    try:
        response = requests.post(url, data=payload)
        data = response.json()
        
        if response.status_code == 200 and "id" in data:
            return {"success": True, "media_id": data["id"]}
        else:
            error_msg = data.get("error", {}).get("message", "Bilinmeyen hata")
            return {"success": False, "message": f"Yayınlama hatası: {error_msg}"}
            
    except Exception as e:
        return {"success": False, "message": f"Bağlantı hatası: {str(e)}"}

def publish_photo(image_url: str, caption: str, user_id=None):
    """
    Wrapper to handle the full flow: Create Container -> Publish.
    """
    # Step 1: Create Container
    step1 = create_media_container(image_url, caption, user_id)
    if not step1["success"]:
        return step1
    
    container_id = step1["container_id"]
    
    # Step 2: Publish
    step2 = publish_media_container(container_id, user_id)
    return step2

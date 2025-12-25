from app import create_app
from app.models.settings import Setting

print("--- Vidos Payment Debugger ---")
try:
    app = create_app()
    with app.app_context():
        print("\n1. Checking Settings DB Connection...")
        # Check API Keys
        key = Setting.get_value('SHOPIER_API_KEY')
        secret = Setting.get_value('SHOPIER_API_SECRET')
        
        print(f"   -> SHOPIER_API_KEY: {'[FOUND]' if key else '[MISSING]'}")
        print(f"   -> SHOPIER_API_SECRET: {'[FOUND]' if secret else '[MISSING]'}")
        
        if key: print(f"      Key Starts With: {key[:5]}...")
        
        print("\n2. Testing Payment Service logic...")
        from app.services.payment_service import ShopierAdapter
        adapter = ShopierAdapter()
        
        if not adapter.api_key:
             print("   -> FAIL: Adapter could not load API Key")
        else:
             print("   -> Adapter loaded API Key successfully")

except Exception as e:
    print(f"\nCRITICAL ERROR: {e}")
    import traceback
    traceback.print_exc()

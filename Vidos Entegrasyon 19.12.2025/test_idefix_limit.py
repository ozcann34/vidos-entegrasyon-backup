import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

from app.services.idefix_service import IdefixClient

def test():
    print("Testing IdefixClient.list_products signature...")
    client = IdefixClient("key", "secret", "vendor")
    try:
        # Trying to call with limit
        # This will fail at network level because keys are fake, 
        # but we want to see if it even accepts the argument.
        client.list_products(page=0, limit=10)
        print("Success: Method accepted 'limit' argument.")
    except TypeError as e:
        print(f"FAILED: {e}")
    except Exception as e:
        # Ignore network errors, they mean the method was called!
        if "unexpected keyword argument 'limit'" in str(e):
            print(f"FAILED: {e}")
        else:
            print(f"Method accepted 'limit' argument (failed later as expected: {type(e).__name__})")

if __name__ == "__main__":
    test()


try:
    with open('app/routes/admin.py', 'rb') as f:
        content = f.read()
    
    # Replace null bytes and decode
    clean_content = content.replace(b'\x00', b'').decode('utf-8', errors='ignore')
    
    # Write back
    with open('app/routes/admin.py', 'w', encoding='utf-8') as f:
        f.write(clean_content)
        
    print("Successfully cleaned admin.py")
    print("-" * 20)
    print(clean_content[:500]) # Print start to verify
except Exception as e:
    print(f"Error: {e}")


path = 'app/services/idefix_service.py'
try:
    with open(path, 'rb') as f:
        content = f.read()
    if b'\x00' in content:
        print(f"File size: {len(content)}, Nulls found.")
        clean = content.replace(b'\x00', b'')
        with open(path, 'wb') as f:
            f.write(clean)
        print(f"Cleaned. New size: {len(clean)}")
    else:
        print("No null bytes found.")
except Exception as e:
    print(e)

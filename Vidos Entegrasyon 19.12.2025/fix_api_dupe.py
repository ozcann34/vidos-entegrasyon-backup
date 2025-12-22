
import os

file_path = 'app/routes/api.py'
with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Target lines: 570 to 749 (1-based)
# Indices: 569 to 749 (exclusive of end index in Python slice logic?)
# lines[569] is line 570.
# lines[748] is line 749.
# We want to keep lines up to 569 (index 0..568)
# And keep lines starting from 749 (index 749..) -> Line 750.

start_idx = 569
end_idx = 749 

print(f"Deleting lines {start_idx+1} to {end_idx}:")
print(f"Start Line Content: {lines[start_idx]}")
print(f"End Line Content (First kept line): {lines[end_idx]}")

if "Trendyol paging" not in lines[start_idx]:
    print("WARNING: Start line does not look like start of bad block!")
    exit(1)

if "idefix" not in lines[end_idx].lower():
    print("WARNING: End line does not look like 'idefix' block!")
    # Check if it's whitespace
    if lines[end_idx].strip() == "":
        print("End line is empty. Checking next line...")
        if "idefix" in lines[end_idx+1].lower():
             print("Found idefix at next line.")
        else:
             print("Still not found idefix.")
             exit(1)
    else:
         print(f"Found: {lines[end_idx]}")
         exit(1)

new_lines = lines[:start_idx] + lines[end_idx:]

with open(file_path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("Successfully deleted duplicate block.")

import pandas as pd
import os

def create_template():
    columns = [
        "Barkod", 
        "Ürün Adı", 
        "Açıklama", 
        "Fiyat", 
        "Satış Fiyatı", 
        "Stok", 
        "Marka", 
        "Kategori", 
        "Stok Kodu", 
        "Renk", 
        "Beden", 
        "Görsel 1", 
        "Görsel 2", 
        "Görsel 3", 
        "Görsel 4", 
        "Görsel 5", 
        "Görsel 6", 
        "Görsel 7"
    ]
    
    df = pd.DataFrame(columns=columns)
    
    # Ensure public directory exists
    public_dir = 'public'
    if not os.path.exists(public_dir):
        os.makedirs(public_dir)
        
    template_path = os.path.join(public_dir, 'template.xlsx')
    
    # Save as Excel
    df.to_excel(template_path, index=False)
    print(f"Template created at {template_path}")

if __name__ == "__main__":
    create_template()

import os
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from io import BytesIO

class ImageTemplateService:
    """
    Service for creating Instagram story templates with overlays
    """
    
    @staticmethod
    def create_story_image(image_url, title, price, discount_price=None, template_style="modern"):
        """
        Downloads a product image and applies a story template overlay.
        
        Args:
            image_url (str): URL of the product image
            title (str): Product title
            price (float): Original price
            discount_price (float, optional): Discounted price
            template_style (str): Template style ('modern', 'clean', 'sale')
            
        Returns:
            str: Path to the generated image file
        """
        try:
            # 1. Download image
            response = requests.get(image_url, stream=True)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content)).convert("RGBA")
            
            # 2. Resize to Instagram Story aspect ratio (1080x1920) or fit within
            target_width = 1080
            target_height = 1920
            
            # Create background (white or blurred version of original)
            background = Image.new('RGBA', (target_width, target_height), (255, 255, 255, 255))
            
            # Resize source image to fit width
            aspect_ratio = img.height / img.width
            new_width = target_width
            new_height = int(new_width * aspect_ratio)
            
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # Center image on background
            y_offset = (target_height - new_height) // 2
            background.paste(img, (0, y_offset), img)
            
            # 3. Apply Overlay based on style
            draw = ImageDraw.Draw(background)
            
            # Load fonts (fallback to default if not found)
            try:
                # Try to load a generic font or system font
                # In a real deployment, we should bundle .ttf files
                font_title = ImageFont.truetype("arial.ttf", 60)
                font_price = ImageFont.truetype("arial.ttf", 80)
                font_small = ImageFont.truetype("arial.ttf", 40)
            except IOError:
                font_title = ImageFont.load_default()
                font_price = ImageFont.load_default()
                font_small = ImageFont.load_default()

            if template_style == "modern":
                # Bottom gradient overlay
                # Draw a semi-transparent black rectangle at the bottom
                overlay = Image.new('RGBA', (target_width, target_height), (0,0,0,0))
                overlay_draw = ImageDraw.Draw(overlay)
                overlay_draw.rectangle([(0, target_height - 600), (target_width, target_height)], fill=(0, 0, 0, 180))
                background = Image.alpha_composite(background, overlay)
                draw = ImageDraw.Draw(background) # Re-init draw on new background
                
                # Text
                text_color = (255, 255, 255)
                
                # Title
                # Simple text wrapping could be added here, for now just slice
                display_title = title[:30] + "..." if len(title) > 30 else title
                draw.text((50, target_height - 500), display_title, font=font_title, fill=text_color)
                
                # Price
                price_text = f"{price:.2f} TL"
                draw.text((50, target_height - 400), price_text, font=font_price, fill=(200, 200, 200) if discount_price else text_color)
                
                # Strike-through if discount
                if discount_price:
                    # Draw red line over old price
                    # (Simplified for now)
                    
                    final_price_text = f"{discount_price:.2f} TL"
                    draw.text((50, target_height - 300), final_price_text, font=font_price, fill=(255, 50, 50))
                    draw.text((50, target_height - 200), "İNDİRİM!", font=font_small, fill=(255, 255, 0))

            # 4. Save
            # 4. Save
            from flask import current_app
            output_dir = os.path.join(current_app.static_folder, 'generated_stories')
            os.makedirs(output_dir, exist_ok=True)
            
            filename = f"story_{int(discount_price or price)}_{os.urandom(4).hex()}.png"
            output_path = os.path.join(output_dir, filename)
            
            background.save(output_path, "PNG")
            return f"/static/generated_stories/{filename}"
            
        except Exception as e:
            print(f"Error creating story image: {e}")
            return None

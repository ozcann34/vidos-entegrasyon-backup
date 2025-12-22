import logging
from difflib import get_close_matches
from app import db
from app.models.mapping import CategoryMapping, BrandMapping

class SmartMatchService:
    def __init__(self):
        pass

    @staticmethod
    def get_category_match(source_category, marketplace, auto_create=False):
        """
        Check DB for existing match. 
        Returns (target_id, target_path) or (None, None).
        source_category: Raw category string from XML "Giyim > Erkek > Pantolon"
        """
        source_category = (source_category or "").strip()
        if not source_category:
            return None, None

        mapping = CategoryMapping.query.filter_by(
            source_category=source_category, 
            marketplace=marketplace
        ).first()

        if mapping:
            return mapping.target_category_id, mapping.target_category_path
        
        return None, None

    @staticmethod
    def save_category_match(source_category, marketplace, target_id, target_path):
        """Save a confirmed match to DB."""
        try:
            source_category = (source_category or "").strip()
            if not source_category: return False

            mapping = CategoryMapping.query.filter_by(
                source_category=source_category, 
                marketplace=marketplace
            ).first()

            if not mapping:
                mapping = CategoryMapping(
                    source_category=source_category,
                    marketplace=marketplace,
                    target_category_id=target_id,
                    target_category_path=target_path
                )
                db.session.add(mapping)
            else:
                mapping.target_category_id = target_id
                mapping.target_category_path = target_path
            
            db.session.commit()
            return True
        except Exception as e:
            logging.error(f"Save category match error: {e}")
            db.session.rollback()
            return False

    @staticmethod
    def get_brand_match(source_brand, marketplace):
        """
        Check DB for existing brand match.
        Returns (target_id, target_name) or (None, None).
        """
        source_brand = (source_brand or "").strip()
        if not source_brand:
            return None, None

        mapping = BrandMapping.query.filter_by(
            source_brand=source_brand,
            marketplace=marketplace
        ).first()

        if mapping:
            return mapping.target_brand_id, mapping.target_brand_name
        
        return None, None

    @staticmethod
    def save_brand_match(source_brand, marketplace, target_id, target_name):
        try:
            source_brand = (source_brand or "").strip()
            if not source_brand: return False

            mapping = BrandMapping.query.filter_by(
                source_brand=source_brand,
                marketplace=marketplace
            ).first()

            if not mapping:
                mapping = BrandMapping(
                    source_brand=source_brand,
                    marketplace=marketplace,
                    target_brand_id=target_id,
                    target_brand_name=target_name
                )
                db.session.add(mapping)
            else:
                mapping.target_brand_id = target_id
                mapping.target_brand_name = target_name
            
            db.session.commit()
            return True
        except Exception as e:
            logging.error(f"Save brand match error: {e}")
            db.session.rollback()
            return False

    # --- INTELLIGENT SUGGESTIONS ---

    @staticmethod
    def suggest_categories(source_category, marketplace_categories_flat, limit=5):
        """
        Suggest categories using difflib.
        marketplace_categories_flat: list of {id, name, path}
        """
        if not source_category or not marketplace_categories_flat:
            return []
        
        # Use path for matching as it contains more context
        target_paths = [c['path'] for c in marketplace_categories_flat]
        
        # 1. Try last part of source (e.g., "Pantolon")
        source_parts = source_category.split('>')
        last_part = source_parts[-1].strip()
        
        matches = get_close_matches(last_part, target_paths, n=limit, cutoff=0.4)
        
        # 2. If weak, try full string
        if len(matches) < limit:
            matches_full = get_close_matches(source_category, target_paths, n=limit, cutoff=0.4)
            matches.extend([m for m in matches_full if m not in matches])
        
        # Map back to objects
        results = []
        for m in matches[:limit]:
            # Find the object for this path (inefficient but safe for small suggestion lists)
            for c in marketplace_categories_flat:
                if c['path'] == m:
                    results.append(c)
                    break
        return results

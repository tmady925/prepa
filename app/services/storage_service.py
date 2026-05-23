import io
import cloudinary
import cloudinary.uploader
from app.core.settings import get_settings

settings = get_settings()

cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
)


class StorageService:

    async def upload_image(self, image_bytes: bytes, folder: str = "prepa") -> str | None:
        """Upload une image sur Cloudinary et retourne l'URL publique."""
        try:
            result = cloudinary.uploader.upload(
                io.BytesIO(image_bytes),
                folder=folder,
                resource_type="image",
                format="png",
            )
            url = result.get("secure_url")
            print(f"Image uploadée: {url}")
            return url
        except Exception as e:
            print(f"Erreur upload Cloudinary: {e}")
            return None


storage_service = StorageService()
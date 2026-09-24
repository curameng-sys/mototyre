"""Uploads user-submitted images (profile pictures, return-evidence photos)
to Cloudinary instead of local disk — Render's own filesystem is wiped on
every deploy, which silently destroyed every photo a customer had ever
uploaded the moment the next code change went live.

Configure with a single CLOUDINARY_URL env var (the "API Environment
variable" shown on the Cloudinary dashboard, shaped like
cloudinary://<api_key>:<api_secret>@<cloud_name>) — the SDK reads it
automatically. The older three-variable form (CLOUDINARY_CLOUD_NAME,
CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET) also still works. Both the
customer and admin app upload their own profile pictures, and the customer
app also uploads return-evidence photos, so both services need this set.
"""
import os
import cloudinary
import cloudinary.uploader

CLOUDINARY_CONFIGURED = bool(os.getenv('CLOUDINARY_URL') or os.getenv('CLOUDINARY_CLOUD_NAME'))

# cloudinary.config() auto-reads CLOUDINARY_URL from the environment on
# import — this call only matters for the three-separate-variables form.
if CLOUDINARY_CONFIGURED and not os.getenv('CLOUDINARY_URL'):
    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
        secure=True,
    )


def upload_image(file, folder):
    """file: a werkzeug FileStorage (already validated by the caller).
    folder: 'profile_pics' or 'return_evidence' — keeps things organized on
    the Cloudinary side the same way they used to be organized on disk.
    Returns the public HTTPS URL, or None if Cloudinary isn't configured
    (falls back to the old local-disk behavior at the call site)."""
    if not CLOUDINARY_CONFIGURED:
        return None
    result = cloudinary.uploader.upload(file, folder=f'mototyre/{folder}')
    return result['secure_url']

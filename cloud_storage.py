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
import re
import logging

logger = logging.getLogger(__name__)


def _clean_cloudinary_url():
    """The Cloudinary SDK reads CLOUDINARY_URL the moment it is imported and crashes the whole app (failed deploy)
    if the value is not exactly cloudinary://<key>:<secret>@<cloud>. Tidy up the usual copy-paste slips first, and if
    it is still not usable, ignore it with a log line instead of taking the site down (photos then go to the database)."""
    raw = os.environ.get('CLOUDINARY_URL')
    if raw is None:
        return
    v = raw.strip().strip('"').strip("'").strip()
    if v.upper().startswith('CLOUDINARY_URL='):          # the key name pasted into the value box
        v = v.split('=', 1)[1].strip().strip('"').strip("'").strip()
    if re.fullmatch(r'cloudinary://[^:@\s]+:[^@\s]+@[^\s/]+', v):
        os.environ['CLOUDINARY_URL'] = v
        return
    os.environ.pop('CLOUDINARY_URL', None)
    logger.error('CLOUDINARY_URL is not in the form cloudinary://<api_key>:<api_secret>@<cloud_name> - ignoring it '
                 '(check the colon between the key and the secret). Photos will be kept in the database instead.')
    print('[cloudinary] CLOUDINARY_URL ignored: it must look like cloudinary://<api_key>:<api_secret>@<cloud_name>')


_clean_cloudinary_url()

import cloudinary            # noqa: E402  (imported after the check above on purpose)
import cloudinary.uploader   # noqa: E402

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
    Returns the public HTTPS URL, or None if Cloudinary isn't configured OR
    the upload itself failed (bad credentials, timeout, Cloudinary outage) —
    every call site already has a local-disk fallback for a None return, so
    one bad upload degrades gracefully instead of crashing the whole request
    (an uncaught exception here used to surface as an opaque 500 — a wrong
    HTML page where the caller's JS expected JSON — and present to the
    customer as a plain 'Network error' with no form actually submitted)."""
    if not CLOUDINARY_CONFIGURED:
        return None
    try:
        result = cloudinary.uploader.upload(file, folder=f'mototyre/{folder}')
        return result['secure_url']
    except Exception:
        logger.exception('Cloudinary upload failed for folder=%s', folder)
        # The failed upload may have partially read the stream — rewind so
        # the caller's own local-disk fallback saves the whole file, not
        # whatever was left unread.
        try:
            file.seek(0)
        except Exception:
            pass
        return None

"""Put a photo on products that have none, from a folder of pictures (kept inside the database, no cloud storage needed).

    set DATABASE_URL=...                                   (the same value the Render service uses)
    python attach_product_images.py mapping.json           # preview only
    python attach_product_images.py mapping.json --apply   # save
    python attach_product_images.py --undo image_backup_YYYYMMDD_HHMMSS.json

mapping.json is  {"<product id>": "<path to the picture>", ...}.  A product that already has a photo is never touched.
Before saving, the ids that get a photo are written to a backup file so --undo can remove exactly those photos again.
"""
import io
import json
import os
import sys
import time
from datetime import datetime

import sqlalchemy as sa
from PIL import Image


def _engine():
    url = os.environ.get('DATABASE_URL', '').strip()
    if not url:
        sys.exit('Set DATABASE_URL first.')
    return sa.create_engine(url)


def _prepare(path):
    """Max 800px, flat white background, JPEG: small enough to keep in the database."""
    im = Image.open(path).convert('RGBA')
    bg = Image.new('RGBA', im.size, 'white')
    bg.alpha_composite(im)
    im = bg.convert('RGB')
    im.thumbnail((800, 800))
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=88)
    return out.getvalue()


def main():
    args = sys.argv[1:]
    eng = _engine()
    if '--undo' in args:
        ids = json.load(open(args[args.index('--undo') + 1]))
        with eng.begin() as c:
            for pid in ids:
                c.execute(sa.text("UPDATE product SET image_url=NULL, image_blob=NULL, image_mime=NULL WHERE id=:i"), {'i': int(pid)})
        print('Removed the photo from %d products.' % len(ids))
        return
    mapping = json.load(open(args[0]))
    apply_ = '--apply' in args
    done = []
    with eng.begin() as c:
        for pid, path in mapping.items():
            row = c.execute(sa.text("SELECT name, image_url, image_blob IS NOT NULL FROM product WHERE id=:i"), {'i': int(pid)}).fetchone()
            if not row:
                print('skip', pid, 'no such product'); continue
            if (row[1] or '').strip() or row[2]:
                print('skip', pid, row[0], '- already has a photo'); continue
            data = _prepare(path)
            print(('save' if apply_ else 'would save'), pid, row[0][:50], '<-', os.path.basename(path), '%d KB' % (len(data) // 1024))
            if apply_:
                c.execute(sa.text("UPDATE product SET image_blob=:b, image_mime='image/jpeg', image_url=:u WHERE id=:i"),
                          {'b': data, 'u': 'db:%d' % int(time.time()), 'i': int(pid)})
                done.append(int(pid))
    if apply_:
        name = 'image_backup_%s.json' % datetime.now().strftime('%Y%m%d_%H%M%S')
        json.dump(done, open(name, 'w'))
        print('Saved %d photos. To remove them again:  python attach_product_images.py --undo %s' % (len(done), name))
    else:
        print('Preview only. Add --apply to save.')


if __name__ == '__main__':
    main()

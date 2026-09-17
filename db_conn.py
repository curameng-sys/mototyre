"""One shared way for the one-off migrate_*.py scripts to reach the
database — reads the exact same DATABASE_URL env var app.py and admin_app.py
use, so a migration can be pointed at any environment (local, Railway,
whatever's next) just by setting that one variable, instead of editing
every script's hardcoded localhost/root/no-password connection by hand."""

import os
from urllib.parse import urlparse
import pymysql

DATABASE_URL = os.getenv('DATABASE_URL', 'mysql+pymysql://root:@localhost:3306/mototyre')


def get_pymysql_connection():
    parsed = urlparse(DATABASE_URL.replace('mysql+pymysql://', 'mysql://', 1))
    return pymysql.connect(
        host=parsed.hostname or 'localhost',
        port=parsed.port or 3306,
        user=parsed.username or 'root',
        password=parsed.password or '',
        database=(parsed.path or '/mototyre').lstrip('/'),
    )

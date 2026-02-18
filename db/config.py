"""Database configuration for Substack PostgreSQL database."""

import os


def get_db_config() -> dict:
    return {
        'host': os.getenv('DB_HOST', 'localhost'),
        'port': int(os.getenv('DB_PORT', '5432')),
        'database': os.getenv('DB_NAME', 'substack'),
        'user': os.getenv('DB_USER', 'postgres'),
        'password': os.getenv('DB_PASSWORD', 'postgres'),
    }


# Config for connecting to default 'postgres' DB (used to CREATE DATABASE)
def get_admin_config() -> dict:
    config = get_db_config()
    config['database'] = 'postgres'
    return config

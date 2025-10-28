import os
import sys
from pathlib import Path
import psycopg2
from dotenv import load_dotenv

# Add project root to path and import central config
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config

def main() -> None:
    """
    Attempts to connect to the PostgreSQL database using credentials from the Config object.
    """
    conn = None
    try:
        # Attempt to connect to the database using the central Config object
        conn = psycopg2.connect(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            dbname=Config.DB_NAME,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD
        )
        print("✅ Success! Connection to PostgreSQL database established.")

    except psycopg2.Error as e:
        print(f"❌ Error: Could not connect to the database. \n   Details: {e}")

    finally:
        # Close the connection if it was successfully created
        if conn is not None:
            conn.close()
            print("Database connection closed.")

if __name__ == "__main__":
    load_dotenv(dotenv_path=project_root / '.env')
    main()
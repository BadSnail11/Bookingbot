
import sqlite3
import os
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "reservations.db")

schema_sql = Path("schema.sql").read_text(encoding="utf-8")

def main():
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(schema_sql)
        # Seed tables if empty
        cur = con.execute("SELECT COUNT(*) FROM tables")
        count = cur.fetchone()[0]
        if count == 0:
            # Example layout: tweak to your venue
            con.executemany(
                "INSERT INTO tables (name, capacity) VALUES (?,?)",
                [
                    ("T1", 2),
                    ("T2", 2),
                    ("T3", 4),
                    ("T4", 4),
                    ("T5", 6),
                    ("VIP1", 8),
                ],
            )
            print("Seeded example tables.")
        con.commit()
        print(f"DB initialized at {DB_PATH}.")
    finally:
        con.close()

if __name__ == "__main__":
    main()

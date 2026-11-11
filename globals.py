# globals.py
import os
from flask import g
import libsql

def query_db(conn, query, args=(), one=False, commit=True):
    # 1. Handle Action Queries (INSERT, UPDATE, DELETE) directly on the Connection
    # We check if it's a select query by seeing if 'select' is at the start of the string
    if not query.strip().lower().startswith("select"):
        # Execute straight on the connection so libsql's internal sync engine tracks it!
        cursor = conn.execute(query, args)
        last_id = getattr(cursor, "lastrowid", None)
        if commit:
            conn.commit()  # This will now successfully sync to Turso Cloud!
        return last_id

    # 2. Handle Search Queries (SELECT) using the standard Cursor pattern
    cursor = conn.cursor()
    cursor.execute(query, args)
    
    # Extract columns cleanly (index 0 gets just the text name string)
    columns = [col[0] for col in cursor.description]
    
    if one:
        row = cursor.fetchone()
        cursor.close()
        if not row: 
            return None
        return dict(zip(columns, row))
    else:
        rows = cursor.fetchall()
        cursor.close()
        if not rows: 
            return []
        return [dict(zip(columns, row)) for row in rows]
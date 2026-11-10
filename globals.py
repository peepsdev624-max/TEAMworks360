import os
from flask import g
import libsql

def query_db(conn, query, args=(), one=False, commit=True):
    cursor = conn.cursor()
    cursor.execute(query, args) # If it's a write action and we want to commit, let's make sure it flushes
    
    # Check if this is an action query (INSERT, UPDATE, DELETE)
    if cursor.description is None:
        if commit:
            try:
                conn.commit()
            except Exception:
                # Catches cases where libsql handles auto-commit natively
                pass
        cursor.close()
        return None

    # SELECT query logic continues safely below...
    columns = [col[0] for col in cursor.description]
    if one:
        row = cursor.fetchone()
        cursor.close()
        # If no row matches, return None cleanly without looping
        if not row: return None
        return dict(zip(columns, row))
    else:
        rows = cursor.fetchall()
        cursor.close()
        if not rows: return []  # Return an empty list if no records match
        return [dict(zip(columns, row)) for row in rows]
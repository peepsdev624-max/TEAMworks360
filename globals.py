import os
from flask import g
import libsql

def query_db(conn, query, args=(), commit=True, one=False):
    cursor = conn.cursor()
    cursor.execute(query, args) # If it's a write action and we want to commit, let's make sure it flushes
    
    if cursor.description is None:
        if commit:
            try:
                conn.commit()
            except Exception:
                # Some versions of libsql handle auto-commit natively and throw an error or ignore manual commits.
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
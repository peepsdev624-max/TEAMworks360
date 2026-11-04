import os
from flask import g
import libsql

def query_db(conn, query, args=(), commit=True, one=False):
    # Set commit=False if running multiple statements as a single transaction.

    cursor = conn.cursor()
    cursor.execute(query, args)
    
    # Check if this is an action query (INSERT, UPDATE, DELETE), if so save changes and exit
    if cursor.description is None:
        if commit:
            conn.commit() # Save changes immediately if requested
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
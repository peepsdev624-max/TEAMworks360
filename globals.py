import os
from flask import g
import libsql

def query_db(conn, query, args=(), one=False):
    cursor = conn.cursor()
    cursor.execute(query, args)
    
    # 1. Check if the query returned any metadata description at all
    if cursor.description is None:
        # For an action query (INSERT/UPDATE), save changes and exit
        conn.commit()
        cursor.close()
        return None

    # 2. Extract columns safely
    columns = [col[0] for col in cursor.description]
    
    if one:
        row = cursor.fetchone()
        cursor.close()
        # If no row matches, return None cleanly without looping
        if not row:
            return None
        return dict(zip(columns, row))
    else:
        rows = cursor.fetchall()
        cursor.close()
        if not rows:
            return []  # Return an empty list if no records match
<<<<<<< HEAD
        return [dict(zip(columns, row)) for row in rows]
=======
        return [dict(zip(columns, row)) for row in rows]
>>>>>>> 893fa0f878bfadf06c8f3c64093b5b986f8e7a73

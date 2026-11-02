  def query_db(conn, query, args=(), one=False):
    # Unified database helper. Returns a list of dicts, a single dict (if one=True), or None.
    
    cursor = conn.cursor()
    cursor.execute(query, args)
    
    if one:
        row = cursor.fetchone()
        cursor.close()
        if not row:
            return None
        columns = [col for col in cursor.description]
        return dict(zip(columns, row))
    else:
        rows = cursor.fetchall()
        cursor.close()
        if not rows:
            return []
        columns = [col for col in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

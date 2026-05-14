import psycopg

try:
    conn = psycopg.connect("postgresql://localhost:5432/call_agent")
    print("Success: postgresql://localhost:5432/call_agent")
    conn.close()
except Exception as e:
    print(f"Failed default: {e}")

try:
    conn = psycopg.connect("postgresql://postgres@localhost:5432/call_agent")
    print("Success: postgresql://postgres@localhost:5432/call_agent")
    conn.close()
except Exception as e:
    print(f"Failed postgres: {e}")

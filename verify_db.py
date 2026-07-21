import pymysql

# Connect to MySQL
conn = pymysql.connect(
    host='127.0.0.1',
    port=3306,
    user='scrape_user',
    password='scrape_password',
    database='scrape_db'
)

cursor = conn.cursor()

# Count total books
cursor.execute("SELECT COUNT(*) as total FROM hamelyn_books")
total = cursor.fetchone()[0]
print(f"✓ Total books in database: {total}")

# Show sample books
print("\n✓ Sample books:")
cursor.execute("SELECT id, title, price, author FROM hamelyn_books LIMIT 5")
for row in cursor.fetchall():
    book_id, title, price, author = row
    print(f"  [{book_id}] {title} - {price} | {author[:60]}")

# Show distinct count
cursor.execute("SELECT COUNT(DISTINCT title) FROM hamelyn_books")
unique_titles = cursor.fetchone()[0]
print(f"\n✓ Unique book titles: {unique_titles}")

cursor.close()
conn.close()
print("\n✓ Database verification complete!")

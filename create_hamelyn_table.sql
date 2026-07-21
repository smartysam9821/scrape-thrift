-- Create Hamelyn books table
CREATE TABLE IF NOT EXISTS hamelyn_books (
    id INT AUTO_INCREMENT PRIMARY KEY,
    url VARCHAR(1000),
    title VARCHAR(500) NOT NULL,
    author VARCHAR(255),
    price VARCHAR(50),
    description TEXT,
    isbn VARCHAR(50),
    publisher VARCHAR(255),
    publish_year VARCHAR(50),
    format VARCHAR(100),
    pages VARCHAR(50),
    language VARCHAR(50),
    availability VARCHAR(255),
    scraped_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_title (title),
    INDEX idx_isbn (isbn),
    INDEX idx_author (author),
    INDEX idx_scraped_at (scraped_at),
    UNIQUE KEY unique_url_title (url, title)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Sample query to view data
-- SELECT * FROM hamelyn_books ORDER BY scraped_at DESC LIMIT 100;

-- Query to find duplicate books
-- SELECT title, COUNT(*) as count FROM hamelyn_books GROUP BY title HAVING count > 1;

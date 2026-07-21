from playwright.async_api import async_playwright
import asyncio
import json


async def debug_page():
    url = "https://tienda.hamelyn.mx/libros/literatura-ficcion/clasicos"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print(f"Loading {url}...")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        
        print("Page loaded. Waiting 3 more seconds...")
        await asyncio.sleep(3)
        
        # Get page HTML
        content = await page.content()
        print(f"\nPage HTML length: {len(content)}")
        
        # Check for articles
        articles = await page.query_selector_all('article')
        print(f"Found {len(articles)} article elements")
        
        # Check for article with itemtype
        articles_schema = await page.query_selector_all('article[itemtype="https://schema.org/Product"]')
        print(f"Found {len(articles_schema)} articles with schema.org/Product")
        
        # Check various other selectors
        selectors_to_try = [
            'article',
            'div[class*="product"]',
            'div[class*="book"]',
            'div[class*="pc"]',
            'div[class*="card"]',
            'li[class*="product"]',
            'section > div > div'
        ]
        
        print("\nTrying various selectors:")
        for selector in selectors_to_try:
            elements = await page.query_selector_all(selector)
            print(f"  {selector}: {len(elements)} elements")
        
        # Check if there's a container with specific classes
        main_content = await page.query_selector('main')
        if main_content:
            print("\nFound main element")
            # Get all divs in main
            divs = await main_content.query_selector_all('div')
            print(f"Found {len(divs)} divs in main")
        
        # Try to find any element with text "Lazarillo"
        lazarillo = await page.query_selector('text=Lazarillo')
        if lazarillo:
            print("\nFound 'Lazarillo' text on page")
            # Get parent
            parent = await page.evaluate('''() => {
                const elem = document.evaluate(
                    "//text()[contains(., 'Lazarillo')]",
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;
                if (elem && elem.parentElement) {
                    return {
                        parent: elem.parentElement.tagName,
                        parentClass: elem.parentElement.className,
                        parentParent: elem.parentElement.parentElement?.tagName,
                        parentParentClass: elem.parentElement.parentElement?.className
                    };
                }
                return null;
            }''')
            print(f"Lazarillo parent info: {json.dumps(parent, indent=2)}")
        else:
            print("\n'Lazarillo' text NOT found on page")
        
        # Save first 5000 chars of HTML for inspection
        with open('debug_page_source.html', 'w', encoding='utf-8') as f:
            f.write(content[:5000])
        print("\nSaved first 5000 chars of HTML to debug_page_source.html")
        
        await browser.close()


if __name__ == "__main__":
    asyncio.run(debug_page())

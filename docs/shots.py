"""Capture full-page screenshots of ClearPath key states."""
import asyncio
from playwright.async_api import async_playwright

URL = "http://localhost:8090/"
D = "/Users/adel/Desktop/GHAI/clearpath/docs"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1320, "height": 1000},
                                  device_scale_factor=2)
        page = await ctx.new_page()
        await page.goto(URL)
        await page.wait_for_timeout(1500)

        # escalated denial with full trace + review actions
        await page.click("text=PA-1002 ·")
        await page.wait_for_selector(".decision", timeout=25000)
        await page.wait_for_timeout(2500)
        await page.screenshot(path=f"{D}/shot-escalated.png", full_page=True)

        # an auto-approved case with trace
        await page.click("text=PA-1001 ·")
        await page.wait_for_selector(".decision", timeout=25000)
        await page.wait_for_timeout(2500)
        await page.screenshot(path=f"{D}/shot-approved.png", full_page=True)
        print("shots done")
        await ctx.close()
        await b.close()


asyncio.run(main())

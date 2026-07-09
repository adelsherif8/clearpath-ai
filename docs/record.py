"""Record a ClearPath AI demo video with Playwright."""
import asyncio
from playwright.async_api import async_playwright

URL = "http://localhost:8090/"
OUT = "/Users/adel/Desktop/GHAI/clearpath/docs/video"


async def click_sample(page, pa_id, wait_ms=3200):
    await page.click(f"text={pa_id} ·")
    try:
        await page.wait_for_selector(".decision", timeout=25000)
    except Exception:
        pass
    await page.wait_for_timeout(wait_ms)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1280, "height": 1150},
                                  record_video_dir=OUT,
                                  record_video_size={"width": 1280, "height": 1150},
                                  device_scale_factor=2)
        page = await ctx.new_page()
        await page.goto(URL)
        await page.wait_for_timeout(1800)

        # 1) clear approve -> auto-resolved
        await click_sample(page, "PA-1001")
        # 2) denial -> escalated to human review (queue fills)
        await click_sample(page, "PA-1002")
        await page.wait_for_timeout(800)
        # 3) reviewer sends it back with a correction -> self-correction loop
        try:
            await page.fill("#revnotes",
                            "Correction: patient DID complete 8 weeks of PT and NSAIDs "
                            "with no relief. Please reconsider.")
            await page.wait_for_timeout(700)
            await page.click("button.snd")
            await page.wait_for_selector(".decision", timeout=25000)
            await page.wait_for_timeout(3500)
        except Exception:
            pass
        # 4) scroll up to show final state
        await page.evaluate("window.scrollTo({top:0,behavior:'smooth'})")
        await page.wait_for_timeout(1500)

        await ctx.close()
        await b.close()
        print("recorded")


asyncio.run(main())
